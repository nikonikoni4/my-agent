"""文件系统工具的寄存器驱动测试。

与原 lifeprism 实现的差异是本次移植的重点：不做路径白名单/可搜索后缀校验，
因此这些用例刻意使用临时目录等任意路径。
"""

from __future__ import annotations

import json

import pytest
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import ToolResult

from lifeprismevalue.tools import build_lifeprism_tools
from lifeprismevalue.tools.base import SUCCESS


def call(name: str, args: dict) -> RawToolCall:
    return RawToolCall(id="t1", name=name, arguments=json.dumps(args, ensure_ascii=False))


def make_register() -> ToolRegister:
    register = ToolRegister()
    register.register(build_lifeprism_tools())
    return register


def _json_tail(r: ToolResult):
    assert not r.is_error, r.content
    return json.loads(r.content[len(SUCCESS):])


@pytest.mark.asyncio
async def test_read_file_ok_and_not_found(tmp_path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    register = make_register()

    payload = _json_tail(await register.execute(call("read_file", {"file_path": str(p)})))
    assert payload["read_ratio"] == 1.0
    assert payload["last_line"] == 2
    assert "第一行" in payload["content"]

    r = await register.execute(call("read_file", {"file_path": str(tmp_path / "nope.txt")}))
    assert r.is_error and "不存在" in r.content


@pytest.mark.asyncio
async def test_read_file_range_and_frontmatter(tmp_path) -> None:
    p = tmp_path / "b.md"
    p.write_text("---\ntitle: x\n---\n正文一\n正文二\n", encoding="utf-8")
    register = make_register()

    payload = _json_tail(
        await register.execute(call("read_file", {"file_path": str(p), "offset": 2, "limit": 1}))
    )
    assert "正文二" in payload["content"] and "正文一" not in payload["content"]

    payload = _json_tail(
        await register.execute(call("read_file", {"file_path": str(p), "only_frontmatter": True}))
    )
    assert "title: x" in payload["content"] and "正文一" not in payload["content"]


@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs_and_reads_back(tmp_path) -> None:
    target = tmp_path / "sub" / "dir" / "new.txt"
    register = make_register()

    r = await register.execute(call("write_file", {"file_path": str(target), "content": "你好"}))
    assert not r.is_error, r.content
    assert target.read_text(encoding="utf-8") == "你好"

    payload = _json_tail(await register.execute(call("read_file", {"file_path": str(target)})))
    assert "你好" in payload["content"]

    # 空内容被拒绝（原实现即如此）
    r = await register.execute(call("write_file", {"file_path": str(tmp_path / "e.txt"), "content": ""}))
    assert r.is_error


@pytest.mark.asyncio
async def test_edit_file_replace_first_and_all(tmp_path) -> None:
    p = tmp_path / "e.txt"
    p.write_text("foo bar foo\n", encoding="utf-8")
    register = make_register()

    r = await register.execute(
        call("edit_file", {"file_path": str(p), "old_content": "foo", "new_content": "baz"})
    )
    assert not r.is_error, r.content
    assert p.read_text(encoding="utf-8") == "baz bar foo\n"

    r = await register.execute(
        call("edit_file", {"file_path": str(p), "old_content": "foo", "new_content": "qux", "replace_all": True})
    )
    assert not r.is_error, r.content
    assert p.read_text(encoding="utf-8") == "baz bar qux\n"

    r = await register.execute(
        call("edit_file", {"file_path": str(p), "old_content": "不存在的内容", "new_content": "x"})
    )
    assert r.is_error and "未找到" in r.content


@pytest.mark.asyncio
async def test_file_tree_py_non_recursive_and_recursive(tmp_path) -> None:
    (tmp_path / "f1.txt").write_text("a", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f2.txt").write_text("b", encoding="utf-8")
    register = make_register()

    r = await register.execute(call("file_tree_py", {"dir_path": str(tmp_path), "recursive": False}))
    assert not r.is_error and "f1.txt" in r.content and "f2.txt" not in r.content

    r = await register.execute(call("file_tree_py", {"dir_path": str(tmp_path), "recursive": True}))
    assert not r.is_error and "f2.txt" in r.content

    r = await register.execute(call("file_tree_py", {"dir_path": str(tmp_path / "nope")}))
    assert r.is_error and "不存在" in r.content

    r = await register.execute(call("file_tree_py", {"dir_path": str(tmp_path / "f1.txt")}))
    assert r.is_error and "不是目录" in r.content


@pytest.mark.asyncio
async def test_search_file_py_match_and_max_depth(tmp_path) -> None:
    (tmp_path / "test_one.txt").write_text("a", encoding="utf-8")
    deep = tmp_path / "d1" / "d2"
    deep.mkdir(parents=True)
    (deep / "test_two.txt").write_text("b", encoding="utf-8")
    register = make_register()

    payload = _json_tail(
        await register.execute(call("search_file_py", {"search_dir": str(tmp_path), "file_name": "test"}))
    )
    assert payload["count"] == 2

    payload = _json_tail(
        await register.execute(
            call("search_file_py", {"search_dir": str(tmp_path), "file_name": "test", "max_depth": 0})
        )
    )
    assert payload["count"] == 1

    r = await register.execute(call("search_file_py", {"search_dir": str(tmp_path / "nope"), "file_name": "test"}))
    assert r.is_error and "不存在" in r.content


@pytest.mark.asyncio
async def test_search_string_py_any_suffix_and_bad_regex(tmp_path) -> None:
    txt = tmp_path / "s.txt"
    txt.write_text("hello world\nfoo bar\n", encoding="utf-8")
    register = make_register()

    r = await register.execute(call("search_string_py", {"path": str(txt), "pattern": "world"}))
    assert not r.is_error and "hello world" in r.content

    # 后缀白名单已移除：.py 等任意文本后缀同样可搜
    py = tmp_path / "s.py"
    py.write_text("print('needle')\n", encoding="utf-8")
    r = await register.execute(call("search_string_py", {"path": str(py), "pattern": "needle"}))
    assert not r.is_error and "needle" in r.content

    r = await register.execute(call("search_string_py", {"path": str(txt), "pattern": "("}))
    assert r.is_error and "正则" in r.content
