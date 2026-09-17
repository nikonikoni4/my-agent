"""假 agent 与假 session 文件：让用例级测试不碰 LLM。

`FakeAgent` 只暴露 driver 真正用到的那几样（`_event_service` / `_session` / `send` /
`persist_session_now` / `cancel`）；`FileFakeAgent` 额外把 session 落成真文件，用来验
「跑完复制改名」；`WriteFakeAgent` 在 send 时按回调往数据根写东西（模拟落库 / 落盘）；
`TurnEndAgent` 可以让某一轮以 error / interrupted 收场。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from myagent.agent.core.provider import Message
from myagent.agent.core.session.types import (
    AssistantMessageData,
    SessionRecordData,
    TurnEndData,
)
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import SESSION_EVENT
from myagent.infra.events.payload import SessionEventPayload

from lifeprismevalue.evalue.case import session_file_path


class FakeAgent:
    """假 agent：用 session/event 模拟一轮（默认每轮 success）。"""

    def __init__(
        self,
        session_id: str = "fake-session",
        reply: str = "已记录",
        emit_events: bool = True,
    ) -> None:
        self._event_service = EventService()
        self._session = SimpleNamespace(meta_data=SimpleNamespace(session_id=session_id))
        self.reply = reply
        self.emit_events = emit_events
        self.sent: list[str] = []
        self.persisted = False
        self.cancelled = False

    async def send(self, text: str) -> None:
        self.sent.append(text)
        if not self.emit_events:
            return
        self._emit(
            "assistant/message",
            AssistantMessageData(message=Message(role="assistant", content=self.reply)),
        )
        self._emit("turn/end", TurnEndData(reason_type="success", reason_text="", error_type=""))

    def _emit(self, type_: str, data) -> None:
        record = SessionRecordData(type=type_, seq=len(self.sent), data=data)
        self._event_service.trigger(SESSION_EVENT, SessionEventPayload(session_record=record))

    def persist_session_now(self) -> None:
        self.persisted = True

    def cancel(self) -> None:
        self.cancelled = True


class FileFakeAgent(FakeAgent):
    """在 send 时落一个真实 session 文件（模拟 SessionPresist 的行为）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.data_path: Path | None = None
        self.session_folder: Path | None = None

    async def send(self, text: str) -> None:
        await super().send(text)
        path = session_file_path(
            self.data_path, self.session_folder, self._session.meta_data.session_id
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("session-line\n", encoding="utf-8")


class WriteFakeAgent(FileFakeAgent):
    """在 send 时按回调往数据根写东西：模拟「被测 agent 真的落库 / 落盘」。

    为什么要它：基线是在跑用例**之前**打的（case.py 的 a 步），所以测试里的写入必须
    发生在跑的过程中，而不是跑之前就把环境改好——那是「先改好再打基线」，改动会被
    快照一起吞掉，测试就永远看不到差异了。
    """

    def __init__(self, write: Callable[[Path], None], **kwargs) -> None:
        super().__init__(**kwargs)
        self.write = write

    async def send(self, text: str) -> None:
        await super().send(text)
        self.write(self.data_path)


class TurnEndAgent(FileFakeAgent):
    """落的 session 文件带指定的 turn 终态（模拟「本轮以 error 收场」）。"""

    def __init__(
        self,
        reason_type: str = "success",
        reason_text: str = "",
        error_type: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.reason_type = reason_type
        self.reason_text = reason_text
        self.error_type = error_type

    async def send(self, text: str) -> None:
        """自己发事件（而非沿用父类恒为 success 的终态），使 collector 与落盘一致。"""
        self.sent.append(text)
        self._emit(
            "assistant/message",
            AssistantMessageData(message=Message(role="assistant", content=self.reply)),
        )
        self._emit(
            "turn/end",
            TurnEndData(
                reason_type=self.reason_type,
                reason_text=self.reason_text,
                error_type=self.error_type,
            ),
        )
        path = session_file_path(
            self.data_path, self.session_folder, self._session.meta_data.session_id
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "turn/end",
                    "turn": 1,
                    "step": None,
                    "data": {
                        "reason_type": self.reason_type,
                        "reason_text": self.reason_text,
                        "error_type": self.error_type,
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


# ---------------- 假 session 文件 ----------------


SESSION_LINES = [
    {"type": "meta_data", "session_id": "s1", "name": "old_agent"},
    {"type": "turn/start", "data": {}},
    {
        "type": "user/message",
        "data": {
            "message": {"role": "user", "content": [{"type": "text", "text": "记录午饭，15.3元"}]}
        },
    },
    {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": "已记录"}}},
    {
        "type": "tool/result",
        "data": {
            "tool_name": "create_custom_record_entry",
            "message": {"role": "tool", "content": "记录成功"},
        },
    },
]

STATS_SESSION_LINES = [
    {"type": "meta_data", "session_id": "s-stats", "name": "old_agent"},
    {"type": "turn/start", "turn": 1, "step": None, "data": {}},
    {"type": "step/start", "turn": 1, "step": 1, "data": {}},
    {
        "type": "user/message",
        "turn": 1,
        "step": 1,
        "data": {"message": {"role": "user", "content": "记录午饭"}},
    },
    {
        "type": "assistant/message",
        "turn": 1,
        "step": 1,
        "data": {
            "message": {"role": "assistant", "content": "已记录"},
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
    },
    {"type": "step/end", "turn": 1, "step": 1, "data": {"reason_type": "success", "reason_text": ""}},
    {
        "type": "turn/end",
        "turn": 1,
        "step": None,
        "data": {"reason_type": "success", "reason_text": ""},
    },
]


def write_session(case_dir: Path, lines: list[dict] | None = None) -> Path:
    """在用例目录里落一个 `session_under_test.jsonl`。"""
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "session_under_test.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in (lines or SESSION_LINES)) + "\n",
        encoding="utf-8",
    )
    return path


def write_turn_session(case_dir: Path, turns: list[dict]) -> Path:
    """落一个只含若干条 turn/end 的 session（模拟被测评 agent 的会话终态）。"""
    return write_session(case_dir, [{"type": "turn/end", **turn} for turn in turns])
