"""命令行入口：离线统计单个 session。

用法：
    python -m lifeprismevalue.stats <session.jsonl>
    python -m lifeprismevalue.stats <session.jsonl> --json [--out result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lifeprismevalue.stats.runner import StatsRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lifeprismevalue.stats",
        description="离线统计单个 session 的 token / 耗时 / 工具调用路径",
    )
    parser.add_argument("session", help="session jsonl 文件路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出合并结果（默认输出文本报告）")
    parser.add_argument("--out", help="额外把合并结果写入指定 JSON 文件")
    args = parser.parse_args(argv)

    path = Path(args.session)
    if not path.exists():
        print(f"session 文件不存在: {path}", file=sys.stderr)
        return 1

    runner = StatsRunner()
    merged = runner.run_path(path)

    print(json.dumps(merged, ensure_ascii=False, indent=2) if args.json else runner.render(merged))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
