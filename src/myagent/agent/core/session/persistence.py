
import asyncio
import json
import logging
from pathlib import Path

from myagent.infra.events import EventService
from myagent.agent.core.session.types import SessionRecordData

logger = logging.getLogger(__name__)


class SessionPresist:
    """
    session的持久化组件，他订阅sesson/event （session.appen）事件进行持久化,不负责加载逻辑
    重要：
    1. 以接口的形式进行持久化，后续如果更换成数据库，jsonl等不同的保存类型，核心代码不必。 -- 暂不实现，先只写file
    2. event事件进入后不马上写入，而是等待200ms | 3条数据以上才一起写入
    3. 写入以批为单位保证不出现半批数据：先序列化整批，再记录文件原大小写入，
       失败时截断回原大小；当场截断失败则记下目标大小，下次写入前先补截断，
       补截断没完成之前绝不写入新数据
    """
    def __init__(self, event_service: EventService, file_path: Path):
        self.file_path = file_path
        event_service.on("session/event", self.cache_data)
        self._buffer = []
        # 上次写入失败后文件应回滚到的字节数；None 表示没有待补的回滚
        self._pending_truncate: int | None = None
        self._presist_loop = asyncio.create_task(self.loop())
        # 循环是后台任务，意外终止时默认无人察觉，这里把异常暴露到日志
        self._presist_loop.add_done_callback(self._on_loop_done)

    def _on_loop_done(self, task: asyncio.Task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("session 持久化循环意外终止", exc_info=task.exception())

    def cache_data(self, record: SessionRecordData):
        """session/event触发，写入缓存"""
        self._buffer.append(record)

    def presist(self):
        """持久化写入函数，将buffer整批写入文件，失败时回滚本次写入并保留buffer"""
        if not self._buffer:
            return
        # 序列化先于打开文件：序列化出错时文件还未被触碰，属于代码 bug，直接抛出
        content = "".join(
            json.dumps(record.to_record_dict(), ensure_ascii=False) + "\n"
            for record in self._buffer
        )
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

    async def loop(self):
        while True:
            await asyncio.sleep(0.2)
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
