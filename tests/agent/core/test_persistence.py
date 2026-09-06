"""SessionPresist 写入失败场景测试。

被测组件：src/myagent/agent/core/session/persistence.py 的 presist() 两级回滚与 loop() 重试。

失败注入方式：替换 Path.open，只拦截组件对自身 file_path 的追加写（'a'）和
回滚截断（'r+b'）两类调用，在 OS 抛出 OSError 的同一入口注入失败。测试自身读文件
（mode='r'）和脚本用尽后的调用全部走真实 open。

不使用真实 OS 级文件锁的原因：msvcrt.locking 只锁已有字节区间，追加写在锁区间之外
仍然成功；磁盘满则无法在测试里稳定复现。组件的契约是"open/write/truncate 抛
OSError 时行为正确"，在该契约边界注入是确定性的做法。

记录用测试替身 FakeRecord：持久化组件只依赖 to_record_dict() 接口，
不绑定 types.py 的具体数据类。
"""
import asyncio
import json
import pathlib
import time

import pytest

import myagent.agent.core.session.persistence as pers
from myagent.agent.core.session.persistence import SessionPresist
from myagent.agent.core.session.types import SessionMetaData


# ---------------------------------------------------------------------------
# 测试替身与工具
# ---------------------------------------------------------------------------

class FakeRecord:
    """持久化组件需要的最小接口：to_record_dict() 返回可 JSON 化的 dict。

    type 固定为 "test"：_conserve_chunk_type 按 type 甄别 chunk，
    非 assistant/chunk 的记录原样通过归并，不影响写入与回滚语义。
    """
    type = "test"

    def __init__(self, payload="x"):
        self.payload = payload

    def to_record_dict(self):
        return {"type": "test", "data": self.payload}


class BadRecord:
    """to_record_dict 返回不可 JSON 化的对象，模拟序列化代码 bug"""
    type = "bad"  # 非 chunk，原样通过 _conserve_chunk_type

    def to_record_dict(self):
        return {"bad": object()}


class PartialWriteHandle:
    """包装真实句柄：write 先落一部分字节再抛 OSError，模拟磁盘满写一半。

    with 块退出时 __exit__ 关闭句柄，已写入的部分字节会真正落到磁盘。
    """

    def __init__(self, handle, cut_ratio=0.5):
        self._h = handle
        self._cut_ratio = cut_ratio

    def write(self, data):
        cut = max(1, int(len(data) * self._cut_ratio))
        self._h.write(data[:cut])
        raise OSError(28, "No space left on device")

    def flush(self):
        self._h.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._h.close()
        return False


def make_presist(path)->SessionPresist:
    """直接构造实例：被测对象是 presist 的写入与回滚，不需要事件订阅和后台循环"""
    p = SessionPresist.__new__(SessionPresist)
    p.file_path = pathlib.Path(path)
    p.meta_data = SessionMetaData(cwd="")  # presist 首次写入（文件不存在）时用它补 meta 行
    p._buffer = []
    p._pending_truncate = None
    return p


def install_open_script(monkeypatch, comp, script):
    """按调用顺序给组件发起的 open 注入脚本。

    script 每项对应组件的一次 open 调用：
      ('partial', ratio) → 真实打开后包装成写一半即抛 OSError 的句柄
      ('raise', err)     → open 本身抛 OSError（模拟文件被占用打不开）
      None               → 真实 open
    脚本用尽后全部走真实 open。只拦截 'a' 与 'r+b' 模式。
    """
    real_open = pathlib.Path.open
    state = {"i": 0}

    def fake_open(self, mode="r", *args, **kwargs):
        if self != comp.file_path or not (mode.startswith("a") or mode == "r+b"):
            return real_open(self, mode, *args, **kwargs)
        i = state["i"]
        state["i"] += 1
        action = script[i] if i < len(script) else None
        if action is None:
            return real_open(self, mode, *args, **kwargs)
        kind, val = action
        if kind == "raise":
            raise val
        handle = real_open(self, mode, *args, **kwargs)
        return PartialWriteHandle(handle, cut_ratio=val)

    monkeypatch.setattr(pers.Path, "open", fake_open)


def batch_of(*records):
    """组件落盘一行时的期望内容，与 presist 的序列化方式一致"""
    return "".join(
        json.dumps(r.to_record_dict(), ensure_ascii=False) + "\n" for r in records
    )


def payload_after_meta(path) -> str:
    """读取文件中 meta 行之后的记录内容（文件由 presist 首次创建时自动补 meta 行）"""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "meta_data" in lines[0], "首行应是 presist 自动补写的 meta 行"
    return "".join(line + "\n" for line in lines[1:])


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_正常写入_整批落盘并清空buffer(tmp_path):
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    r1, r2 = FakeRecord("a"), FakeRecord("b")
    comp._buffer.extend([r1, r2])

    comp.presist()

    assert payload_after_meta(path) == batch_of(r1, r2)
    assert comp._buffer == []
    assert comp._pending_truncate is None


def test_空buffer_不创建文件(tmp_path):
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)

    comp.presist()

    assert not path.exists()


def test_两次追加_内容连续无重复(tmp_path):
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    r1, r2 = FakeRecord("a"), FakeRecord("b")
    comp._buffer.append(r1)
    comp.presist()
    comp._buffer.append(r2)
    comp.presist()

    assert payload_after_meta(path) == batch_of(r1, r2)


# ---------------------------------------------------------------------------
# 收尾：首行 meta 的 updated_at 刷新
# ---------------------------------------------------------------------------

def test_二次写入_收尾原位刷新首行meta的updated_at(tmp_path):
    """非首写的 presist 成功后收尾刷新首行 meta 的 updated_at：
    首行仍是合法 meta 且时间确实更新，第二行起的记录内容一字不动。
    """
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    r1, r2 = FakeRecord("a"), FakeRecord("b")
    comp._buffer.append(r1)
    comp.presist()
    meta_before = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    records_before = payload_after_meta(path)

    time.sleep(0.02)  # 保证两次 updated_at 的时间差可观测
    comp._buffer.append(r2)
    comp.presist()

    meta_after = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert meta_after["type"] == "meta_data"
    assert meta_after["updated_at"] > meta_before["updated_at"]
    assert payload_after_meta(path) == records_before + batch_of(r2)


def test_刷新meta失败_只放弃不抛出_落盘结果不受影响(tmp_path, monkeypatch):
    """收尾刷新 meta 的 open 被占用（OSError）时：presist 不抛异常，
    整批写入与 buffer 清空照常完成。
    """
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    r1, r2 = FakeRecord("a"), FakeRecord("b")
    comp._buffer.append(r1)
    comp.presist()  # 首写：建文件 + meta 行，不触发收尾刷新

    # 第二次 presist 的 open 序列：批量追加('a')走真实，收尾刷新 meta('r+b')被占用
    install_open_script(monkeypatch, comp, [None, ("raise", OSError(13, "Permission denied"))])
    comp._buffer.append(r2)
    comp.presist()  # 不得抛异常

    assert payload_after_meta(path) == batch_of(r1, r2)
    assert comp._buffer == []
    assert comp._pending_truncate is None


# ---------------------------------------------------------------------------
# 失败路径：序列化错误
# ---------------------------------------------------------------------------

def test_序列化错误_文件不被触碰_buffer保留(tmp_path):
    """序列化在打开文件之前完成，出错时文件一个字节都不动，也不留回滚标记"""
    path = tmp_path / "s.jsonl"
    path.write_text("OLD\n", encoding="utf-8")
    comp = make_presist(path)
    comp._buffer.append(BadRecord())

    with pytest.raises(TypeError):
        comp.presist()

    assert path.read_text(encoding="utf-8") == "OLD\n"
    assert len(comp._buffer) == 1
    assert comp._pending_truncate is None


# ---------------------------------------------------------------------------
# 失败路径：写入一半失败
# ---------------------------------------------------------------------------

def test_写入一半失败_当场回滚成功_文件回到原大小(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("OLD\n", encoding="utf-8")
    comp = make_presist(path)
    r1 = FakeRecord("a")
    comp._buffer.append(r1)
    install_open_script(monkeypatch_ctx := pytest.MonkeyPatch(), comp, [("partial", 0.5)])

    try:
        with pytest.raises(OSError):
            comp.presist()
    finally:
        monkeypatch_ctx.undo()

    # 残缺字节被截掉，文件精确回到写入前
    assert path.read_text(encoding="utf-8") == "OLD\n"
    assert comp._pending_truncate is None
    # buffer 保留，重试整批写入且不重复
    comp.presist()
    assert path.read_text(encoding="utf-8") == "OLD\n" + batch_of(r1)


# ---------------------------------------------------------------------------
# 极端场景：写入失败且回滚也失败（文件被长期占用）
# ---------------------------------------------------------------------------

def test_写入回滚都失败_长期占用期间绝不追加_解除后补截断恢复(tmp_path, monkeypatch):
    """极端场景全链路：
    第 1 次写入写一半失败，回滚时文件也被占用打不开；
    之后两次重试期间占用一直没解除（补截断打不开，本次一个字节都不写）；
    最后占用解除：先补截断去掉残缺字节，再整批写入，最终文件无垃圾无重复。
    """
    path = tmp_path / "s.jsonl"
    path.write_text("OLD\n", encoding="utf-8")
    before = path.stat().st_size  # Windows 文本模式 \n 存为 \r\n，以实际字节数为准
    comp = make_presist(path)
    r1 = FakeRecord("a")
    comp._buffer.append(r1)
    locked = OSError(13, "Permission denied")
    install_open_script(
        monkeypatch, comp,
        [
            ("partial", 0.5),   # 第 1 次：追加写，写一半失败
            ("raise", locked),  # 第 1 次回滚截断：文件被占用，打不开
            ("raise", locked),  # 第 2 次尝试的补截断：仍被占用
            ("raise", locked),  # 第 3 次尝试的补截断：仍被占用
            # 之后脚本用尽：占用解除，走真实 open
        ],
    )

    # 第 1 次：写入失败，当场回滚也失败，回滚目标被记住
    with pytest.raises(OSError):
        comp.presist()
    garbage = path.read_bytes()
    assert len(garbage) > before, "残缺字节确实落了盘"
    assert comp._pending_truncate == before
    assert len(comp._buffer) == 1

    # 占用期间的第 2、3 次：补截断打不开直接中止，文件一个字节都不许动
    for _ in range(2):
        with pytest.raises(OSError):
            comp.presist()
        assert path.read_bytes() == garbage, "占用期间绝不许在残缺文件上追加"
        assert comp._pending_truncate == before
        assert len(comp._buffer) == 1

    # 占用解除：先补截断再写入，最终精确等于 原内容 + 整批一次
    # （文件由测试预写 OLD 创建，presist 不会补 meta 行）
    comp.presist()
    assert path.read_text(encoding="utf-8") == "OLD\n" + batch_of(r1)
    assert comp._pending_truncate is None
    assert comp._buffer == []


# ---------------------------------------------------------------------------
# 边界场景
# ---------------------------------------------------------------------------

def test_待回滚时文件被删_跳过补截断_正常重建(tmp_path):
    """文件在"失败"与"恢复"之间被删掉：无需回滚，直接按新文件整批写入。
    文件不存在会触发 presist 自动补 meta 行，故断言用 payload_after_meta。
    """
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    r1 = FakeRecord("a")
    comp._buffer.append(r1)
    comp._pending_truncate = 42  # 模拟上次失败残留的回滚目标，文件此时已不存在

    comp.presist()

    assert payload_after_meta(path) == batch_of(r1)
    assert comp._pending_truncate is None


# ---------------------------------------------------------------------------
# loop 层：重试与存活
# ---------------------------------------------------------------------------

def test_loop_OSError不死循环_等1秒重试一次_继续下一轮(monkeypatch, tmp_path):
    """presist 抛 OSError 时 loop 不终止：每轮 先写→失败→等1秒→重试→失败→进下一轮。
    通过记录 sleep 时长序列验证重试节奏，fake sleep 立即返回避免真实等待。
    presist 被替身替换，本测试不产生真实文件 IO。
    """
    real_sleep = asyncio.sleep
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def main():
        comp = make_presist(tmp_path / "unused.jsonl")
        comp._buffer.append(FakeRecord("x"))
        calls = {"n": 0}
        task_ref = {}

        def failing():
            calls["n"] += 1
            if calls["n"] >= 4:
                task_ref["task"].cancel()
            raise OSError(28, "busy")

        comp.presist = failing
        task_ref["task"] = asyncio.get_running_loop().create_task(comp.loop())
        try:
            await asyncio.wait_for(asyncio.shield(task_ref["task"]), timeout=5)
        except asyncio.CancelledError:
            pass
        return calls["n"]

    n = asyncio.run(main())
    assert n == 4
    # 节奏：轮间 0.2 → 失败 → 重试前等 1 → 重试失败 → 下一轮 0.2 → …
    assert sleeps[:4] == [0.2, 1, 0.2, 1]


# ---------------------------------------------------------------------------
# assistant/chunk 固件：已搬到 conftest.py，与 store 加载测试共用
# ---------------------------------------------------------------------------

from conftest import make_chunk_record_list  # noqa: E402


def test_保存时把assistant_chunk合并为text_chunk再落盘(tmp_path):
    """保存链路：presist 先把 buffer 里的 assistant/chunk 归并为 text-chunk，再整批写入。

    非 chunk 记录原样保留，文件中不再出现 assistant/chunk 行，合并结果与
    test_merge_assistant_chunk 的期望分组一致（9 条 chunk → 5 条 text-chunk）。
    """
    path = tmp_path / "s.jsonl"
    comp = make_presist(path)
    normal = FakeRecord("keep")
    comp._buffer.extend([normal, *make_chunk_record_list()])

    comp.presist()

    lines = [json.loads(line) for line in payload_after_meta(path).splitlines()]
    types = [line["type"] for line in lines]
    assert "assistant/chunk" not in types
    assert types.count("text-chunk") == 5
    assert types[0] == "test"  # 非 chunk 记录原样落盘
    assert comp._buffer == []


def test_merge_assistant_chunk(tmp_path):
    """_merge_chunks 把 assistant/chunk 按同类同槽位归并为 text-chunk。

    固件序列与期望分组见 make_chunk_record_list 的 docstring：
    content×3 → tool-call(index=0)×2 → content×1 → tool-call(index=1)×1 → reasoning×2，
    共合并为 5 条 text-chunk。
    """
    presist = make_presist(tmp_path / "unused.jsonl")
    merged = presist._merge_chunks(make_chunk_record_list())

    assert len(merged) == 5
    # 信封信息取组内首条：seq/timestamp 同理，这里只验 seq 代表分组边界
    assert [r.seq for r in merged] == [1, 4, 6, 7, 8]
    assert [r.data.type for r in merged] == ["content", "tool-call", "content", "tool-call", "reasoning"]
    # index 是槽位号：index=0 是合法值，不得被清成 None
    assert [r.data.index for r in merged] == [None, 0, None, 1, None]
    # id/name 只在组内首条片段携带，合并后保留首条的值
    assert [r.data.id for r in merged] == [None, "call_a", None, "call_b", None]
    assert [r.data.name for r in merged] == [None, "get_weather", None, "get_time", None]

    # 碎片逐片保存：content/reasoning 进 texts，tool-call 进 args，两者互斥
    assert merged[0].data.texts == ["你", "好", "！"]
    assert merged[0].data.args is None
    assert merged[1].data.args == ['{"date"', ': "2026-09-05"}']
    assert merged[1].data.texts is None
    assert merged[2].data.texts == ["晴天"]
    assert merged[3].data.args == ["{}"]
    assert merged[4].data.texts == ["思考", "中"]

    # source_event_seqs 与 uuid 按组归并，保持出现顺序
    assert [r.source_event_seqs for r in merged] == [[1, 2, 3], [4, 5], [6], [7], [8, 9]]
    assert merged[0].data.uuid == ["u1", "u2", "u3"]
    assert merged[1].data.uuid == ["u4", "u5"]
    assert merged[4].data.uuid == ["u8", "u9"]
    # first_seq 与信封 seq 一致，dt 首片为 0
    assert all(r.data.first_seq == r.seq for r in merged)
    assert all(r.data.dt[0] == 0 for r in merged)
    # dt 是组内相邻片段的毫秒间隔（固件 timestamp 按 seq 每 1ms 递增，相邻恒为 1ms）
    assert merged[0].data.dt == [0, 1.0, 1.0]
    assert merged[4].data.dt == [0, 1.0]