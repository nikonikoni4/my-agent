
import asyncio
import datetime
import json
import logging
from pathlib import Path

from myagent.infra.events import EventService
from myagent.infra.events.eventspec import SessionEventPayload
from myagent.agent.core.session.types import SessionRecordData,TextChunkData,AssistantChunkData,SessionMetaData

logger = logging.getLogger(__name__)


class SessionPresist:
    """
    session的持久化组件，他订阅sesson/event （session.appen）事件进行持久化,不负责加载逻辑
    重要：
    1. 以接口的形式进行持久化，后续如果更换成数据库，jsonl等不同的保存类型，核心代码不必。 -- 暂不实现，先只写file
    2. event事件进入后不马上写入，而是等待2秒
    3. 写入以批为单位保证不出现半批数据：先序列化整批，再记录文件原大小写入，
       失败时截断回原大小；当场截断失败则记下目标大小，下次写入前先补截断，
       补截断没完成之前绝不写入新数据
    """
    def __init__(self, event_service: EventService, file_path: Path, meta_data: SessionMetaData):
        self.file_path = file_path
        # 会话目录可能从未创建（新项目/新环境首次落盘），不建目录直接 append 会 FileNotFoundError
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_data = meta_data
        event_service.register("session/event", self.cache_data)
        self._buffer :list[SessionRecordData] = []
        # 上次写入失败后文件应回滚到的字节数；None 表示没有待补的回滚
        self._pending_truncate: int | None = None
        self._presist_loop = asyncio.create_task(self.loop())
        # 循环是后台任务，意外终止时默认无人察觉，这里把异常暴露到日志
        self._presist_loop.add_done_callback(self._on_loop_done)

    def _on_loop_done(self, task: asyncio.Task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("session 持久化循环意外终止", exc_info=task.exception())

    def cache_data(self, payload: SessionEventPayload):
        """session/event触发，写入缓存。

        事件携带的是 SessionEventPayload（内含记录的深拷贝），buffer 存的必须是
        SessionRecordData 本体，否则 presist 时访问 record.type 会直接报错。
        """
        self._buffer.append(payload.session_record)

    def _conserve_chunk_type(self):
        """将assistant/chunk 类型 的recorddata -> text-chunk 进行合并"""
        merged_buffer = [] # 构造新的buffer
        pending: list[SessionRecordData] = []  # 当前连续的 assistant/chunk 段
        for record in self._buffer:
            if record.type == "assistant/chunk":
                pending.append(record)
                continue
            if pending:
                # 合并
                merged_buffer.extend(self._merge_chunks(pending))
                pending = []
            merged_buffer.append(record)
        if pending:
            merged_buffer.extend(self._merge_chunks(pending))
        self._buffer = merged_buffer
        
    def _merge_chunks(self,assistant_records:list[SessionRecordData])->list[SessionRecordData]:
        """
        把一些assistant/chunk 的 record 改为 data 合并为 TextChunkData 的 record
        
        """
        assert assistant_records is not None , "传入的assistant_records为空"
        # 先以第一个开始
        assistant_data : AssistantChunkData= assistant_records[0].data
        text_chunk_records  = []
        text_chunk_records.append(SessionRecordData(
                type="text-chunk",
                seq = assistant_records[0].seq,
                turn = assistant_records[0].turn,
                step= assistant_records[0].step,
                timestamp=assistant_records[0].timestamp,
                source_event_seqs=[assistant_records[0].seq],
                data = TextChunkData(
                    type = assistant_data.type,
                    dt = [0],
                    first_seq=assistant_records[0].seq,
                    # index=0 是合法槽位号，falsy 判断会把它写成 None，导致后续同槽片段无法归组
                    index = assistant_data.index if assistant_data.index or assistant_data.index == 0 else None,
                    id=assistant_data.id if assistant_data.id else None,
                    name = assistant_data.name if assistant_data.name else None,
                    # args 必须与 uuid/dt/source_event_seqs 等长对齐：首片只带 id/name 无参数时记 None 占位
                    args=[assistant_data.args] if assistant_data.type == "tool-call" else None,
                    texts=[assistant_data.texts] if assistant_data.type in ("content", "reasoning") else None,
                    finish_reason=[assistant_data.finish_reason] if assistant_data.type == "finish" else None
                )
            )
        )

        # dt 是组内相邻片段的毫秒间隔，追加时参照组内上一条记录
        last_record = assistant_records[0]
        for record in assistant_records[1:]:
            data :AssistantChunkData = record.data
            if data.type == text_chunk_records[-1].data.type and data.index == text_chunk_records[-1].data.index:
                # 追加
                text_chunk_records[-1].data.dt.append((record.timestamp - last_record.timestamp).total_seconds() * 1000)
                text_chunk_records[-1].source_event_seqs.append(record.seq)
                if data.type == "tool-call":
                    # 组首片段可能只带 id/name（args 为 None），追加前先把列表建出来；
                    # None 也要占位追加，保持 args 与 uuid/dt/source_event_seqs 等长对齐
                    if text_chunk_records[-1].data.args is None:
                        text_chunk_records[-1].data.args = []
                    text_chunk_records[-1].data.args.append(record.data.args)
                elif data.type == "finish":
                    text_chunk_records[-1].data.finish_reason.append(record.data.finish_reason)
                else:
                    if text_chunk_records[-1].data.texts is None:
                        text_chunk_records[-1].data.texts = []
                    text_chunk_records[-1].data.texts.append(record.data.texts)
            else:
                # 重新新建一条
                text_chunk_records.append(SessionRecordData(
                        type="text-chunk",
                        seq = record.seq,
                        turn = record.turn,
                        step= record.step,
                        timestamp=record.timestamp,
                        source_event_seqs=[record.seq],
                        data = TextChunkData(
                            type = record.data.type,
                            dt = [0],
                            first_seq=record.seq,
                            index = record.data.index if record.data.index or record.data.index == 0 else None,
                            id=record.data.id if record.data.id else None,
                            name=record.data.name if record.data.name else None,
                            args=[record.data.args] if record.data.type == "tool-call" else None,
                            texts=[record.data.texts] if record.data.type in ("content", "reasoning") else None,
                            finish_reason=[record.data.finish_reason] if record.data.type == "finish" else None
                        )
                    )
                )
            # 新建分支的组首就是当前记录，统一推进参照点
            last_record = record
        return text_chunk_records

    def presist(self):
        """持久化写入函数，将buffer整批写入文件，失败时回滚本次写入并保留buffer"""
        if not self._buffer:
            return
        # 序列化前先归并：assistant/chunk 在文件里以 text-chunk 打包行存储
        self._conserve_chunk_type()
        # 序列化先于打开文件：序列化出错时文件还未被触碰，属于代码 bug，直接抛出
        content = "".join(
            json.dumps(record.to_record_dict(), ensure_ascii=False) + "\n"
            for record in self._buffer
        )
        # 首次写入（文件不存在）时先落 meta 行，meta 与记录同批写入，回滚逻辑统一覆盖
        first_write = not self.file_path.exists()
        if first_write:
            content = json.dumps(self.meta_data.meta_data(), ensure_ascii=False) + "\n" + content
        # 有待补的回滚：先把文件截回上次写入前的大小。文件已消失则无需回滚。
        # 截不动（打不开文件）就抛异常中止本次写入，绝不在残缺数据上追加
        if self._pending_truncate is not None:
            if self.file_path.exists():
                with self.file_path.open('r+b') as f:
                    f.truncate(self._pending_truncate)
            self._pending_truncate = None
        before = self.file_path.stat().st_size if self.file_path.exists() else 0
        try:
            with self.file_path.open('a', encoding='utf-8') as f:
                f.write(content)
                f.flush()
        except OSError:
            # 先记下回滚目标，再尽力当场截断；当场截不动（比如文件被锁）
            # 就留给下次写入前补，保证下次不会在残缺文件上直接追加
            self._pending_truncate = before
            try:
                if self.file_path.exists():
                    with self.file_path.open('r+b') as f:
                        f.truncate(before)
                    self._pending_truncate = None
            except OSError:
                pass
            raise 
        self._buffer = []  # 整批落盘成功才清空
        # 收尾：非首写时刷新首行 meta 的 updated_at（首写的 meta 行时间就是新的）
        if not first_write:
            self._update_meta_line()

    def _update_meta_line(self):
        """尽力刷新文件首行 meta 的 updated_at，非关键收尾步骤，只尝试一次。

        任何 OSError（文件被占用、被删等）直接放弃：不重试、不抛出、不进入
        写入回滚记账。重写整个文件代价太大，这里按旧首行等长原位覆盖：
        沿用旧行的行尾符（\n 或 \r\n），新行变短用空格补在行尾符之前，
        变长或首行不是 meta 行则跳过，保证第二行起的内容永不被触碰。
        """
        try:
            self.meta_data.updated_at = datetime.datetime.now(datetime.timezone.utc)
            new_bytes = json.dumps(self.meta_data.meta_data(), ensure_ascii=False).encode("utf-8")
            with self.file_path.open("r+b") as f:
                old_line = f.readline()
                try:
                    is_meta = json.loads(old_line).get("type") == "meta_data"
                except (ValueError, AttributeError):
                    is_meta = False
                if not is_meta or not old_line.endswith(b"\n"):
                    return
                ending = b"\r\n" if old_line.endswith(b"\r\n") else b"\n"
                old_body = old_line[: -len(ending)]
                if len(new_bytes) > len(old_body):
                    logger.warning("meta 行变长，无法原位刷新 updated_at，跳过")
                    return
                f.seek(0)
                f.write(new_bytes + b" " * (len(old_body) - len(new_bytes)) + ending)
        except OSError as e:
            logger.warning("刷新 meta updated_at 失败（不影响已落盘记录）: %s", e)

    async def loop(self):
        while True:
            await asyncio.sleep(2)
            try:
                self.presist()
            except OSError as e:
                logger.warning("session 持久化写入失败，1 秒后重试一次: %s", e)
                await asyncio.sleep(1)
                try:
                    self.presist()
                except OSError as e2:
                    # buffer 保留，等下一轮循环继续重试
                    logger.error("session 持久化重试仍失败，保留 %d 条待下轮写入: %s",
                                 len(self._buffer), e2)