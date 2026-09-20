"""tool_use_guard 的路径白名单护栏测试。

护栏面对一批 tool/call 时，对**每一条**只有两种结果，用例按其组织：

- **反对**：这条 call 的路径落在白名单外 → 其 call_id 出现在裁决表里
- **不反对**：路径在册 / 不归护栏管（非受管工具、参数没解析成 dict、路径参数缺失）

另有两组用例守着护栏在事件链上的位置：

- **不认领整批**：本批没有反对意见时也必须 await _next()，否则需要整批视野的下游
  订阅方永远收不到事件；有反对意见时同样照走下游，把下游对其他 call 的反对并进来
- **不改输入**：payload 与其中的 arguments dict 都是 provider 的输出，后面 session
  记录与回喂模型还要用

路径一律取 tmp_path 下的真实目录，不捏造 "D:/x/y" 这类字符串：护栏的判据是
resolve() 后的绝对路径，只有真实路径才能验证它算出的落点与工具实际会写到的地方
是同一个。同时真实路径也让它跨平台可跑（Windows 大小写不敏感那一条除外，单独
skipif 标注）。
"""

from __future__ import annotations

import copy
import sys

import pytest

from myagent.agent.guard.tool_use_guard import ToolUseGuard
from myagent.infra.events import EventService
from myagent.infra.events.payload import ToolCallInfo, ToolCallPayload

from lifeprismevalue.tools import build_lifeprism_tools

_DENIED = "deny"


class _ChainTail:
    """链尾订阅方替身：记被调用次数，并可指定自己返回的反对意见表。

    两种调用形态都受着：护栏内部是 `await _next()`（无参），事件服务派发时是
    `callback(payload, _next)`（订阅方约定最后一个参数是 _next），故签名写成可选参。
    默认返回 None，即"链上再无人认领"——护栏据此判断该不该合并。
    """

    def __init__(self, verdict=None) -> None:
        self.verdict = verdict
        self.calls = 0

    async def __call__(self, payload=None, _next=None):
        self.calls += 1
        return self.verdict


@pytest.fixture
def workspace(tmp_path):
    """白名单根：user/ 与 agent/ 在册，dataset/ 不在册。"""
    for name in ("user", "agent", "dataset"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def guard(workspace) -> ToolUseGuard:
    """只把 workspace/user 与 workspace/agent 圈进白名单。"""
    return ToolUseGuard(
        {"allow_path": [str(workspace / "user"), str(workspace / "agent")]}
    )


def make_call(call_id: str, tool_name: str, tool_params) -> ToolCallInfo:
    """造一条 call（tool_params 可以是解析好的 dict，也可以是未解析的 str）。"""
    return ToolCallInfo(id=call_id, name=tool_name, arguments=tool_params)


def make_payload(*calls: ToolCallInfo) -> ToolCallPayload:
    # 护栏只读 tool_call_requests，不碰 session：本用例传 None 表达"用不到"
    return ToolCallPayload(tool_call_requests=list(calls), session=None)


# ==========================================
# 反对：路径落在白名单外
# ==========================================


@pytest.mark.asyncio
async def test_白名单外的路径被反对(guard, workspace):
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("c1", "read_file", {"file_path": str(workspace / "dataset" / "x.db")})
        ),
        tail,
    )

    assert r["c1"]["decision"] == _DENIED
    assert str(workspace / "dataset" / "x.db") in r["c1"]["reason"], "拒绝理由要带上被拒的路径"


@pytest.mark.asyncio
async def test_相对路径穿越出白名单被反对(guard, workspace):
    """user/../dataset/x.db 落在白名单外。

    这条是"前缀匹配不能写成子串包含"的核心用例：路径里确实含有白名单条目的字面量
    （"…\\user"），子串判断会放行，只有先 resolve() 再比路径段才拦得住。
    """
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call(
                "c1", "read_file", {"file_path": str(workspace / "user" / ".." / "dataset" / "x.db")}
            )
        ),
        tail,
    )

    assert r["c1"]["decision"] == _DENIED


@pytest.mark.asyncio
async def test_伪前缀目录不被误放行(guard, workspace):
    """workspace/userdata 不是 workspace/user 的子路径，只是名字里带 user。

    同样是子串判断会漏、路径段判断才对的一例。
    """
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(make_call("c1", "read_file", {"file_path": str(workspace / "userdata" / "a.md")})),
        tail,
    )

    assert r["c1"]["decision"] == _DENIED


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"allow_path": []}, {"allow_path": None}])
async def test_没配白名单时全部反对(config, workspace):
    """fail-closed：漏配白名单应挡住一切，而不是静默放行一切。"""
    g = ToolUseGuard(config)
    r = await g.file_sys_path_guard(
        make_payload(make_call("c1", "read_file", {"file_path": str(workspace / "user" / "a.md")})),
        _ChainTail(),
    )

    assert r["c1"]["decision"] == _DENIED
    assert "未配置" in r["c1"]["reason"], "漏配要能一眼从理由里看出来是配置问题"


# ==========================================
# 逐条判：一批里只反对该反对的那些
# ==========================================


@pytest.mark.asyncio
async def test_一批里只反对白名单外的那几条(guard, workspace):
    """裁决表只装被拒的 call，缺席即不反对；结果按 call_id 索引，与批内顺序无关。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("在册1", "read_file", {"file_path": str(workspace / "user" / "a.md")}),
            make_call("越界1", "write_file", {"file_path": str(workspace / "dataset" / "b.md")}),
            make_call("越界2", "edit_file", {"file_path": "C:/Windows/win.ini"}),
            make_call("在册2", "search_file_py", {"search_dir": str(workspace / "agent")}),
        ),
        tail,
    )

    assert set(r) == {"越界1", "越界2"}
    assert all(v["decision"] == _DENIED for v in r.values())


@pytest.mark.asyncio
async def test_空批次返回空表(guard):
    """空批=没有反对意见，仍然是有效裁决（区别于 None 的"无人认领"）。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(make_payload(), tail)

    assert r == {}
    assert tail.calls == 1


# ==========================================
# 不反对：白名单内的路径
# ==========================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rel_path",
    ["user/a.md", "user/sub/b.md", "agent/chat/bootstrap.md", "user"],
    ids=["文件", "子目录里的文件", "另一条白名单条目下", "白名单条目本身"],
)
async def test_白名单内的路径不被反对(guard, workspace, rel_path):
    """这些路径全是文件夹工具的目标，故用 write_file——目标文件不存在也要能判。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("c1", "write_file", {"file_path": str(workspace / rel_path), "content": "x"})
        ),
        tail,
    )

    assert r == {}, "白名单内不该有反对意见"
    assert tail.calls == 1, "本批没有反对意见也必须委派下游，不能把整批认领掉"


@pytest.mark.asyncio
async def test_白名单条目可以是单个文件(workspace):
    """白名单条目写成文件路径时，只放行该文件本身，不连带放行同目录其他文件。"""
    target = workspace / "user" / "u.md"
    target.write_text("x", encoding="utf-8")
    g = ToolUseGuard({"allow_path": [str(target)]})

    r = await g.file_sys_path_guard(
        make_payload(
            make_call("在册", "read_file", {"file_path": str(target)}),
            make_call("同目录其他文件", "read_file", {"file_path": str(workspace / "user" / "other.md")}),
        ),
        _ChainTail(),
    )

    assert set(r) == {"同目录其他文件"}


@pytest.mark.skipif(sys.platform != "win32", reason="大小写不敏感是 Windows 路径语义")
@pytest.mark.asyncio
async def test_Windows下白名单大小写不敏感(guard, workspace):
    """Windows 上路径大小写不敏感，大小写不同的同一路径不该被误拦。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(make_call("c1", "read_file", {"file_path": str(workspace / "USER" / "a.md")})),
        tail,
    )

    assert r == {}
    assert tail.calls == 1


# ==========================================
# 覆盖范围：filesystem.py 里所有带路径参数的工具
# ==========================================


# 与实现里的映射表相互独立地列一遍：漏改 / 误删某一行时这条能报出来
_PATH_TOOLS = [
    ("read_file", "file_path"),
    ("write_file", "file_path"),
    ("edit_file", "file_path"),
    ("file_tree_py", "dir_path"),
    ("search_file_py", "search_dir"),
    ("search_string_py", "path"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,param_name", _PATH_TOOLS)
async def test_每个带路径的工具都受管(guard, workspace, tool_name, param_name):
    """白名单外的路径 + 白名单内的路径各走一次，确认这个工具确实过护栏。"""
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("越界", tool_name, {param_name: str(workspace / "dataset" / "x")}),
            make_call("在册", tool_name, {param_name: str(workspace / "user" / "x")}),
        ),
        _ChainTail(),
    )

    assert set(r) == {"越界"}, f"{tool_name} 的 {param_name} 没被护栏拦住"


@pytest.mark.asyncio
async def test_覆盖了filesystem里全部带路径参数的工具(guard, workspace):
    """从工具实例的 schema 反推该管哪些工具，与实现里的手写映射表互相校验。

    新加一个带路径参数的文件工具而忘了补映射表时，这条会失败——漏管的工具等于
    白名单上的一个洞，值得用一条会自己发现新工具的用例守住。
    """
    path_param_names = {"file_path", "dir_path", "search_dir", "path"}
    covered = []
    for tool in build_lifeprism_tools():
        if type(tool).__module__ != "lifeprismevalue.tools.filesystem":
            continue
        properties = tool.parameters.get("properties", {})
        param_name = next((p for p in properties if p in path_param_names), None)
        if param_name is None:
            continue
        covered.append(tool.name)
        r = await guard.file_sys_path_guard(
            make_payload(
                make_call("越界", tool.name, {param_name: str(workspace / "dataset" / "x")})
            ),
            _ChainTail(),
        )
        assert r, f"filesystem 里的 {tool.name} 带路径参数 {param_name}，却没受管"

    assert sorted(covered) == sorted(name for name, _ in _PATH_TOOLS), (
        f"filesystem 里带路径参数的工具是 {sorted(covered)}"
    )


# ==========================================
# 不反对：护栏不该管的调用
# ==========================================


@pytest.mark.asyncio
async def test_参数未解析成功时跳过护栏(guard, workspace):
    """provider 尽力解析后仍是 str = 这个调用本来就走不通执行路径。

    跳过它不产生"绕过护栏执行危险动作"的口子（见 ADR 2026-09-10 决策 3），故不反对。
    """
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("c1", "read_file", '{"file_path": "' + str(workspace / "dataset" / "x.db"))
        ),
        tail,
    )

    assert r == {}
    assert tail.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{}, {"file_path": None}, {"file_path": ""}, {"file_path": 123}, {"file_path": ["a"]}],
    ids=["没这个参数", "值缺失", "空串", "值不是字符串", "值是列表"],
)
async def test_路径参数不成立时交给工具层报错(guard, params):
    """护栏只负责"路径准不准"，"参数缺没缺"是工具层参数校验的职责，别在这儿越权拒绝。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(make_payload(make_call("c1", "read_file", params)), tail)

    assert r == {}
    assert tail.calls == 1


@pytest.mark.asyncio
async def test_非受管工具不出现在裁决表里(guard, workspace):
    """数据库类工具的参数名里也可能出现 path 字样，但不归路径护栏管。"""
    tail = _ChainTail()
    r = await guard.file_sys_path_guard(
        make_payload(
            make_call("c1", "query_user_habits", {"path": str(workspace / "dataset" / "x.db")})
        ),
        tail,
    )

    assert r == {}
    assert tail.calls == 1


# ==========================================
# 不改输入
# ==========================================


@pytest.mark.asyncio
async def test_不修改输入payload(guard, workspace):
    """payload 是 provider 的输出，session 记录与回喂模型都还要用，护栏只能读。

    arguments 是 dict 形态（provider 已解析）时一并比对内容——就地改那个 dict
    同样算改了输入。
    """
    payload = make_payload(
        make_call("在册", "read_file", {"file_path": str(workspace / "user" / "a.md")}),
        make_call("越界", "write_file", {"file_path": str(workspace / "dataset" / "b.md"), "content": "x"}),
        make_call("未解析", "read_file", '{"file_path": "坏 JSON'),
    )
    before = copy.deepcopy(payload)

    await guard.file_sys_path_guard(payload, _ChainTail())

    assert payload == before, "护栏不得修改输入 payload"


# ==========================================
# 与事件链的接合
# ==========================================


@pytest.mark.asyncio
async def test_作为waterfall订阅方接入事件链(guard, workspace):
    """真事件服务上跑一遍：护栏是 async 订阅方，裁决表沿链合并。

    顺带盯住契约——订阅方若被写成同步函数，waterfall 会记 ERROR 后返回 None，
    这条用例会以"护栏的反对意见丢了"的形式失败
    （见 docs/coding-rules/2026-09-18-waterfall订阅契约.md）。
    """
    service = EventService()
    tail = _ChainTail({"被下游拒的": {"decision": _DENIED, "reason": "下游的理由"}})
    service.register("tool/call", guard.file_sys_path_guard)  # 先注册者在最外层
    service.register("tool/call", tail)

    r = await service.waterfall(
        "tool/call",
        make_payload(
            make_call("被护栏拒的", "read_file", {"file_path": str(workspace / "dataset" / "x.db")}),
            make_call("被下游拒的", "read_file", {"file_path": str(workspace / "user" / "a.md")}),
        ),
    )

    assert set(r) == {"被护栏拒的", "被下游拒的"}, "护栏的反对与下游的反对都要在裁决表里"
    assert r["被护栏拒的"]["reason"] != "下游的理由", (
        "同一条被两层都反对时保留外层（先注册者）的理由"
    )
    assert tail.calls == 1, "护栏不认领整批——有反对意见时下游同样要跑"


@pytest.mark.asyncio
async def test_链上无人认领时返回空表而非None(guard, workspace):
    """链尾没有订阅方时，护栏给的是空表而不是 None。

    None 在本仓库的 waterfall 语义里是"无人认领"（request/error 那边据此抛
    AgentUnclaimedError），若护栏在本批无反对时返回 None，调用方会把"护栏看过了、
    没意见"误读成"没人管这批调用"。
    """
    service = EventService()
    service.register("tool/call", guard.file_sys_path_guard)

    r = await service.waterfall(
        "tool/call",
        make_payload(make_call("c1", "read_file", {"file_path": str(workspace / "user" / "a.md")})),
    )

    assert r == {}
