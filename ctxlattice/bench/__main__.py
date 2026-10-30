"""``python -m ctxlattice.bench`` —— 跑完三档 transcript 并写出报告。"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

from ..policy import Policy
from .harness import run_all


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]          # -> 仓库根
    ap = argparse.ArgumentParser(
        prog="python -m ctxlattice.bench",
        description="用同一份 transcript 跑两臂，报输入降幅 / 成本降幅 / 回本点。")
    ap.add_argument("--transcripts", default=str(root / "bench" / "transcripts"))
    ap.add_argument("--out", default=None, help="默认 bench/results/<日期>-<label>")
    ap.add_argument("--label", default="run")
    ap.add_argument("--delta", type=float, default=0.1)
    ap.add_argument("--price", type=float, default=1.0)
    ap.add_argument("--budget", type=int, default=Policy().budget_tokens)
    ap.add_argument("--interval", type=int, default=Policy().compact_interval)
    ap.add_argument("--keep", type=int, default=Policy().verbatim_keep)
    ap.add_argument("--max-turns", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑名字含该串的 transcript")
    ap.add_argument("--sqlite", action="store_true", help="B 臂改用 SQLite 存储（默认内存）")
    ap.add_argument("--session-len", type=int, default=0,
                    help="每多少轮算一次新会话（0=单会话）。会话首轮缓存冷启动")
    args = ap.parse_args(argv)

    out_root = Path(args.out) if args.out else (
        root / "bench" / "results" /
        ("%s-%s" % (_dt.date.today().isoformat(), args.label)))

    policy = Policy(budget_tokens=args.budget,
                    compact_interval=args.interval,
                    verbatim_keep=args.keep)
    results = run_all(transcript_dir=args.transcripts, out_root=out_root,
                      label=args.label, policy=policy,
                      delta=args.delta, price=args.price, use_sqlite=args.sqlite,
                      session_len=args.session_len, max_turns=args.max_turns,
                      only=args.only)

    print("结果写入 %s" % out_root)
    _modes = sorted({r.summary.mode for r in results})
    if _modes != ["exact"]:
        print("⚠️  mode=%s —— 本表是**估算口径**（无 tiktoken），" % ",".join(_modes))
        print("    数字不与 mode=exact 的历史结果可比；δ 敏感性不可用于对外结论。")
    print("%-28s %-6s %-6s %10s %10s %10s %10s" %
          ("transcript", "档位", "轮数", "输入降幅", "成本δ=.1", "成本δ=1", "回本点"))
    for r in results:
        be = r.summary.break_even_turn
        print("%-28s %-6s %6d %9.1f%% %9.1f%% %9.1f%% %10s" % (
            r.transcript, r.locality, len(r.rows_a),
            r.summary.input_saving_pct, r.summary.cost_saving_pct,
            r.summary_by_delta[1.0].cost_saving_pct,
            ("第%d轮" % be) if be else "未回本"))
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
