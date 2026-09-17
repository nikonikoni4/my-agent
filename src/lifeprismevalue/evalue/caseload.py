"""用例加载：读 cases.yaml → CaseSet（含结构与取值校验）。

字段含义见 lifeprismTestData/README.md。
只做"加载 + 校验"，不做执行；执行入口见 runner.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lifeprismevalue.evalue.types import (
    DB_MODE_EMPTY,
    DB_MODES,
    INPUT_MODE_AGENT,
    INPUT_MODE_SCRIPTED,
    INPUT_MODES,
    JUDGE_MODE_MODEL,
    JUDGE_MODES,
    Case,
    CaseSet,
    DbConfig,
    EnvConfig,
    Judge,
    Meta,
    Precondition,
    Simulator,
    Turn,
)


class CaseLoadError(Exception):
    """用例文件缺失或格式非法。"""


def load_case_set(path: str | Path) -> CaseSet:
    """读取一个 cases.yaml，返回 CaseSet。

    Raises:
        CaseLoadError: 文件不存在、不是合法 YAML、缺必填字段、字段取值非法，
            或用例 id 重复。
    """
    p = Path(path)
    if not p.exists():
        raise CaseLoadError(f"用例文件不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CaseLoadError(f"YAML 解析失败: {e}") from e
    if not isinstance(raw, dict):
        raise CaseLoadError("用例文件顶层必须是 mapping（meta / cases）")

    meta = _build_meta(raw.get("meta"))

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseLoadError("cases 必须是非空列表")

    cases: list[Case] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw_cases):
        cases.append(_build_case(item, meta, idx, seen))

    raw_notes = raw.get("notes") or []
    if not isinstance(raw_notes, list):
        raise CaseLoadError("notes 必须是列表")

    return CaseSet(meta=meta, cases=cases, notes=[str(n) for n in raw_notes], path=str(p))


# ---------------- 内部构造（逐层校验） ----------------


def _build_meta(raw: Any) -> Meta:
    if not isinstance(raw, dict):
        raise CaseLoadError("缺少 meta 或 meta 不是 mapping")
    meta_id = raw.get("id")
    if not meta_id:
        raise CaseLoadError("meta.id 必填")
    return Meta(
        id=str(meta_id),
        env=_build_env_config(raw.get("env")),
        name=str(raw.get("name") or ""),
        dataset_version=int(raw.get("dataset_version") or 1),
        premise=str(raw.get("premise") or ""),
        expectation=str(raw.get("expectation") or ""),
        multi_turn=bool(raw.get("multi_turn") or False),
        data=str(raw.get("data") or ""),
        grading=str(raw.get("grading") or ""),
        source=str(raw.get("source") or ""),
    )


def _build_env_config(raw: Any) -> EnvConfig:
    """解析 `meta.env`：这份用例文件的初始状态声明（必填）。

    这里只校验「本层能校验的」：结构、取值、非空。至于「声明的路径 / 表在底座里
    是否真的存在」，要看底座才知道，由 runner 开槽前用 `env.check_env_inputs` 校验。
    两层都不省：本层拦住写法错误，那一层拦住「底座变了 / 名字打错」。
    """
    if raw is None:
        raise CaseLoadError(
            "meta.env 必填：声明本次环境从底座复制什么、库怎么来（见 README 的 env 一节）"
        )
    if not isinstance(raw, dict):
        raise CaseLoadError("meta.env 必须是 mapping")

    copy = raw.get("copy")
    if not isinstance(copy, list) or not copy:
        raise CaseLoadError(
            "meta.env.copy 必须是非空列表：环境要有初始状态的必需来源（提示词等），"
            "空清单会造出一个没有提示词的环境"
        )
    extra = raw.get("extra") or []
    if not isinstance(extra, list):
        raise CaseLoadError("meta.env.extra 必须是列表")
    return EnvConfig(
        copy=[_rel_path(item, "meta.env.copy") for item in copy],
        extra=[_rel_path(item, "meta.env.extra") for item in extra],
        db=_build_db_config(raw.get("db")),
    )


def _rel_path(item: Any, where: str) -> str:
    """路径项必须是相对底座的路径（绝对路径会指到环境之外，等于把环境当成宿主机用）。"""
    text = str(item or "").strip()
    if not text:
        raise CaseLoadError(f"{where} 里有空路径")
    if Path(text).is_absolute():
        raise CaseLoadError(f"{where} 必须是相对底座的路径: {text}")
    return text


def _build_db_config(raw: Any) -> DbConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CaseLoadError("meta.env.db 必须是 mapping")
    if not raw.get("path"):
        raise CaseLoadError("meta.env.db.path 必填（相对底座的数据根路径）")
    mode = str(raw.get("mode") or DB_MODE_EMPTY)
    if mode not in DB_MODES:
        raise CaseLoadError(f"meta.env.db.mode 非法: {mode}（应为 {DB_MODES}）")
    keep = raw.get("keep_tables") or []
    if not isinstance(keep, list):
        raise CaseLoadError("meta.env.db.keep_tables 必须是列表")
    return DbConfig(
        path=_rel_path(raw["path"], "meta.env.db.path"),
        mode=mode,
        keep_tables=[str(t).strip() for t in keep if str(t).strip()],
    )


def _build_case(raw: Any, meta: Meta, idx: int, seen: set[str]) -> Case:
    where = f"cases[{idx}]"
    if not isinstance(raw, dict):
        raise CaseLoadError(f"{where} 必须是 mapping")

    case_id = raw.get("id")
    if not case_id:
        raise CaseLoadError(f"{where} 缺 id")
    case_id = str(case_id)
    if case_id in seen:
        raise CaseLoadError(f"用例 id 重复: {case_id}")
    seen.add(case_id)
    where = f"用例 {case_id}"

    ctype = raw.get("type")
    if not ctype:
        raise CaseLoadError(f"{where} 缺 type")

    input_mode = str(raw.get("input_mode") or INPUT_MODE_SCRIPTED)
    if input_mode not in INPUT_MODES:
        raise CaseLoadError(f"{where} input_mode 非法: {input_mode}（应为 {INPUT_MODES}）")

    turns = _build_turns(raw.get("turns"), where)
    if input_mode == INPUT_MODE_SCRIPTED and not turns:
        raise CaseLoadError(f"{where} input_mode=scripted 时 turns 不能为空")

    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise CaseLoadError(f"{where} evidence 必须是非空列表")

    rubric = raw.get("rubric")
    if not rubric or not str(rubric).strip():
        raise CaseLoadError(f"{where} 缺 rubric")

    simulator = _build_simulator(raw.get("simulator"), where)
    if input_mode == INPUT_MODE_AGENT and simulator is None:
        raise CaseLoadError(f"{where} input_mode=agent 时必须提供 simulator")

    return Case(
        id=case_id,
        type=str(ctype),
        turns=turns,
        evidence=[str(e) for e in evidence],
        rubric=str(rubric),
        precondition=_build_precondition(raw.get("precondition"), where),
        # 单条用例未写时，沿用 meta 的默认
        multi_turn=bool(raw["multi_turn"]) if "multi_turn" in raw else meta.multi_turn,
        input_mode=input_mode,
        simulator=simulator,
        judge=_build_judge(raw.get("judge"), where),
    )


def _build_turns(raw: Any, where: str) -> list[Turn]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CaseLoadError(f"{where} turns 必须是列表")
    turns: list[Turn] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("text"):
            raise CaseLoadError(f"{where} turns[{i}] 缺 text")
        turns.append(
            Turn(
                text=str(item["text"]),
                role=str(item.get("role") or "user"),
                trigger=str(item["trigger"]) if item.get("trigger") else None,
            )
        )
    return turns


def _build_precondition(raw: Any, where: str) -> Precondition:
    if raw is None:
        return Precondition()
    if not isinstance(raw, dict):
        raise CaseLoadError(f"{where} precondition 必须是 mapping")
    rules = raw.get("rules") or []
    if not isinstance(rules, list):
        raise CaseLoadError(f"{where} precondition.rules 必须是列表")
    fixture = raw.get("fixture") or {}
    if not isinstance(fixture, dict):
        raise CaseLoadError(f"{where} precondition.fixture 必须是 mapping")
    return Precondition(rules=[str(r) for r in rules], fixture=dict(fixture))


def _build_simulator(raw: Any, where: str) -> Simulator | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CaseLoadError(f"{where} simulator 必须是 mapping")
    facts = raw.get("known_facts") or []
    if not isinstance(facts, list):
        raise CaseLoadError(f"{where} simulator.known_facts 必须是列表")
    handover = raw.get("handover_after") or 0
    if not isinstance(handover, int) or isinstance(handover, bool) or handover < 0:
        raise CaseLoadError(f"{where} simulator.handover_after 必须是非负整数")
    return Simulator(
        goal=str(raw.get("goal") or ""),
        known_facts=[str(f) for f in facts],
        handover_after=handover,
    )


def _build_judge(raw: Any, where: str) -> Judge:
    if raw is None:
        return Judge()
    if not isinstance(raw, dict):
        raise CaseLoadError(f"{where} judge 必须是 mapping")
    mode = str(raw.get("mode") or JUDGE_MODE_MODEL)
    if mode not in JUDGE_MODES:
        raise CaseLoadError(f"{where} judge.mode 非法: {mode}（应为 {JUDGE_MODES}）")
    return Judge(mode=mode)
