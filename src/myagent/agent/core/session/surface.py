from myagent.agent.core.session.types import SessionRecordData
from bisect import bisect_left, bisect_right
    
    
class SurfaceManager:
    def __init__(self,record_list : list[SessionRecordData]|None = None):
        self.current_seq = 0
        self.node = []
        if record_list:
            self.refresh_node(record_list)

    def refresh_node(self,record_list:list[SessionRecordData])->list:
        """
        record_list必须按照seq的大小升序排序
        增量处理：从current_seq之后的新记录开始重放，已有的node保留不清空
        """
        i = self.current_seq
        while i < len(record_list):
            record = record_list[i]
            if record.surface_op :
                if record.surface_op == "append":
                    self.node.append(record.seq)
                else:
                    start = record.surface_op.get("start")
                    end = record.surface_op.get("end")
                    lo = bisect_left(self.node, start)   # 第一个 >= start 的下标
                    hi = bisect_right(self.node, end)    # 最后一个 <= end 的下标 + 1
                    if self.node[lo:hi] != record.source_event_seqs:
                        raise ValueError(f"record_listf[{record.seq}]的surface_op： { record.surface_op} 无法与 source_event_seqs： {record.source_event_seqs}对齐")
                    del self.node[lo:hi]
            self.current_seq = record.seq
            i+=1

        return self.node
    