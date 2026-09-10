"""tests/agent/core 的共享测试固件。

chunk 固件被 persistence（merge 单元）与 store（加载解包往返）两组测试共用，
放在 conftest 中避免跨文件复制。
"""
import datetime

from myagent.agent.core.session.types import AssistantChunkData, SessionRecordData
from myagent.agent.core.provider import StreamChunk

_CHUNK_BASE_TIME = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def make_chunk_record(seq: int, chunk: StreamChunk, *, turn: int = 1, step: int = 1,
                      timestamp: datetime.datetime | None = None,
                      uuid: str | None = None) -> SessionRecordData:
    """把一个 StreamChunk 包成一条 assistant/chunk 的 SessionRecordData。

    uuid/timestamp 缺省按 seq 推导（u{seq}、基准时间 + seq 毫秒），同批记录
    时间单调递增且可预期，验证 dt 与合并分组时不用逐条手填。
    """
    return SessionRecordData(
        type="assistant/chunk",
        seq=seq,
        turn=turn,
        step=step,
        uuid=uuid if uuid is not None else f"u{seq}",
        timestamp=timestamp if timestamp is not None else _CHUNK_BASE_TIME + datetime.timedelta(milliseconds=seq),
        data=AssistantChunkData(chunk),
    )


def make_chunk_record_list() -> list[SessionRecordData]:
    """覆盖 _merge_chunks 全部分支的典型序列，seq 从 1 连续递增。

    期望合并为 6 条 text-chunk（type 和 index 都相同才归并）：
      seq1-3  content            连续同类 → 1 条，texts=["你", "好", "！"]
      seq4-5  tool-call index=0  与前面 type 不同 → 新建 1 条；两条同组合并，
                                 args=['{"date"', ': "2026-09-05"}']
      seq6    content            → 1 条
      seq7    tool-call index=1  并行调用的第二个槽位，index 不同 → 1 条
      seq8-9  reasoning          连续同类 → 1 条，texts=["思考", "中"]
      seq10   finish             → 1 条，finish_reason=["stop"]（独占字段，不占 texts）
    """
    specs = [
        StreamChunk(content="你"),
        StreamChunk(content="好"),
        StreamChunk(content="！"),
        StreamChunk(tool_index=0, tool_id="call_a", tool_name="get_weather",
                    tool_arguments_delta='{"date"'),
        StreamChunk(tool_index=0, tool_arguments_delta=': "2026-09-05"}'),
        StreamChunk(content="晴天"),
        StreamChunk(tool_index=1, tool_id="call_b", tool_name="get_time",
                    tool_arguments_delta="{}"),
        StreamChunk(reasoning_content="思考"),
        StreamChunk(reasoning_content="中"),
        StreamChunk(finish_reason="stop"),
    ]
    return [make_chunk_record(seq=i + 1, chunk=c) for i, c in enumerate(specs)]
