import pytest

from myagent.agent.core.session.surface import SurfaceManager
from myagent.agent.core.session.types import (
    SessionData,
    SessionRecordData,
    TurnStartData,
)


def make_record(seq: int, surface_op=None, source_event_seqs=None,
                data: SessionData | None = None) -> SessionRecordData:
    """构造测试用记录，只填 SurfaceManager 关心的字段，data 用占位实例。"""
    return SessionRecordData(
        type="user/message",
        seq=seq,
        surface_op=surface_op,
        source_event_seqs=source_event_seqs,
        data=data if data is not None else TurnStartData(turn=1),
    )


class TestInit:
    """测试 __init__：初始状态与全量重放。"""

    def test_init_without_records(self):
        """测试场景：不传记录时 node 为空、current_seq 为 0"""
        m = SurfaceManager()
        assert m.node == []
        assert m.current_seq == 0

    def test_init_with_none(self):
        """测试场景：显式传 None 时 node 为空、current_seq 为 0"""
        m = SurfaceManager(None)
        assert m.node == []
        assert m.current_seq == 0

    def test_init_with_empty_list(self):
        """测试场景：传空列表时 node 为空、current_seq 为 0"""
        m = SurfaceManager([])
        assert m.node == []
        assert m.current_seq == 0

    def test_init_ignores_records_without_surface_op(self):
        """测试场景：surface_op 为 None 的记录不进 node，但 current_seq 仍推进到其 seq"""
        records = [make_record(1), make_record(2)]
        m = SurfaceManager(records)
        assert m.node == []
        assert m.current_seq == 2

    def test_init_collects_append_seqs(self):
        """测试场景：append 记录按顺序收集自身 seq"""
        records = [
            make_record(1, surface_op="append"),
            make_record(3, surface_op="append"),
            make_record(5, surface_op="append"),
        ]
        m = SurfaceManager(records)
        assert m.node == [1, 3, 5]
        assert m.current_seq == 5

    def test_init_replace_removes_aligned_range(self):
        """测试场景：replace 记录删除 [start, end] 闭区间内已收集的 seq"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op="append"),
            make_record(4, surface_op={"op": "replace", "start": 2, "end": 3},
                        source_event_seqs=[2, 3]),
        ]
        m = SurfaceManager(records)
        assert m.node == [1]
        assert m.current_seq == 4

    def test_init_replace_range_is_inclusive(self):
        """测试场景：replace 的 start 和 end 都是闭区间边界，两端元素一并删除"""
        records = [
            make_record(seq, surface_op="append") for seq in range(1, 6)
        ] + [
            make_record(6, surface_op={"op": "replace", "start": 2, "end": 4},
                        source_event_seqs=[2, 3, 4]),
        ]
        m = SurfaceManager(records)
        assert m.node == [1, 5]

    def test_init_replace_misaligned_source_event_seqs_raises(self):
        """测试场景：source_event_seqs 与实际范围内的 seq 不一致时抛 ValueError"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op={"op": "replace", "start": 1, "end": 2},
                        source_event_seqs=[1, 9]),  # 实际是 [1, 2]
        ]
        with pytest.raises(ValueError):
            SurfaceManager(records)

    def test_init_multi_round_compaction(self):
        """测试场景：多轮压缩交错重放——append、replace、再 append、再 replace"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op="append"),
            make_record(4, surface_op={"op": "replace", "start": 1, "end": 2},
                        source_event_seqs=[1, 2]),
            make_record(5, surface_op="append"),
            make_record(6, surface_op={"op": "replace", "start": 3, "end": 5},
                        source_event_seqs=[3, 5]),
        ]
        m = SurfaceManager(records)
        assert m.node == []
        assert m.current_seq == 6


class TestRefreshNodeIncremental:
    """测试 refresh_node 的增量语义：保留已有 node，只处理 current_seq 之后的新记录。"""

    def test_refresh_empty_list_returns_empty_node(self):
        """测试场景：空管理器刷新空列表，返回空 node"""
        m = SurfaceManager()
        assert m.refresh_node([]) == []
        assert m.current_seq == 0

    def test_refresh_returns_node_itself(self):
        """测试场景：返回值就是 self.node 同一个列表对象"""
        m = SurfaceManager([make_record(1, surface_op="append")])
        assert m.refresh_node([make_record(1, surface_op="append")]) is m.node

    def test_incremental_refresh_retains_existing_node(self):
        """测试场景：二次刷新不清空旧内容，新 append 追加在后面"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op="append"),
        ]
        m = SurfaceManager(records)
        records.append(make_record(4, surface_op="append"))
        m.refresh_node(records)
        assert m.node == [1, 2, 3, 4]
        assert m.current_seq == 4

    def test_incremental_refresh_processes_new_replace(self):
        """测试场景：新追加的 replace 记录作用于完整 node，删除已收集的 seq"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op="append"),
        ]
        m = SurfaceManager(records)
        records.append(make_record(4, surface_op={"op": "replace", "start": 2, "end": 3},
                                   source_event_seqs=[2, 3]))
        m.refresh_node(records)
        assert m.node == [1]
        assert m.current_seq == 4

    def test_incremental_refresh_after_replace_tail_does_not_reprocess(self):
        """测试场景：以 replace 结尾的列表再次刷新不重放该 replace，不抛异常且 node 不变"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op={"op": "replace", "start": 1, "end": 2},
                        source_event_seqs=[1, 2]),
        ]
        m = SurfaceManager(records)
        assert m.node == []
        m.refresh_node(records)  # 修复前这里会重放 replace 并抛 ValueError
        assert m.node == []
        assert m.current_seq == 3

    def test_incremental_refresh_advances_past_records_without_surface_op(self):
        """测试场景：新记录没有 surface_op 时 node 不变，current_seq 仍推进"""
        records = [
            make_record(1, surface_op="append"),
        ]
        m = SurfaceManager(records)
        records.append(make_record(2))
        records.append(make_record(3))
        m.refresh_node(records)
        assert m.node == [1]
        assert m.current_seq == 3

    def test_refresh_is_idempotent(self):
        """测试场景：同一列表反复刷新结果不变（幂等）"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
            make_record(3, surface_op={"op": "replace", "start": 1, "end": 2},
                        source_event_seqs=[1, 2]),
            make_record(4, surface_op="append"),
        ]
        m = SurfaceManager(records)
        expected = list(m.node)
        for _ in range(3):
            assert m.refresh_node(records) == expected
        assert m.current_seq == 4

    def test_incremental_mixed_scenario(self):
        """测试场景：多轮增量刷新交错出现 append、replace、无 surface_op 记录"""
        records = [
            make_record(1, surface_op="append"),
            make_record(2, surface_op="append"),
        ]
        m = SurfaceManager(records)
        assert m.node == [1, 2]

        records.append(make_record(3))  # 无 surface_op，只推进 current_seq
        m.refresh_node(records)
        assert m.node == [1, 2]

        records.append(make_record(4, surface_op="append"))
        records.append(make_record(5, surface_op={"op": "replace", "start": 1, "end": 2},
                                   source_event_seqs=[1, 2]))
        m.refresh_node(records)
        assert m.node == [4]
        assert m.current_seq == 5

        records.append(make_record(6, surface_op="append"))
        m.refresh_node(records)
        assert m.node == [4, 6]
        assert m.current_seq == 6
