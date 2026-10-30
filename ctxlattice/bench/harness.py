"""两臂对跑 + 出报告（MEASUREMENT-SPEC §7 的固定形状）。

设计上只有一条要求：**A 臂与 B 臂之间除了上下文装配方式，不允许有任何差异**。
所以这里的每一处都必须对称 —— 同一个 system、同一个 counter、同一份 policy、
同一份 transcript、同一个 δ 与单价。任何一处不对称，测出来的都不是方案。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..arms import ArmProtocol, BaselineArm, LatticeArm, assert_arms_aligned
from ..model import Role
from ..policy import Policy
from ..session import make_system_prompt
from ..store import MemoryStore
from ..tokens import CounterMode, make_counter
from . import metrics
from .metrics import DELTA_GRID, TurnCost
from .transcripts import Transcript, load_transcript


@dataclass(frozen=True, slots=True)
class RunResult:
    transcript: str
    locality: str
    summary: metrics.Summary
    summary_by_delta: dict[float, metrics.Summary]
    boundary: dict
    rows_a: tuple[TurnCost, ...]
    rows_b: tuple[TurnCost, ...]
    out_dir: Path
    mode: str


def _make_arms(transcript: Transcript, policy: Policy, counter, *, use_sqlite: bool,
               db_path: Path | None):
    system = make_system_prompt(None)
    arm_a = BaselineArm(system=system, counter=counter, policy=policy)
    if use_sqlite:
        from ..store_sqlite import SqliteStore
        assert db_path is not None
        store = SqliteStore(str(db_path))
        store.open()
    else:
        store = MemoryStore()
    arm_b = LatticeArm(store=store, system=system, counter=counter, policy=policy)
    assert_arms_aligned(arm_a, arm_b)
    return arm_a, arm_b, store


def run(
    transcript: Transcript,
    *,
    policy: Policy | None = None,
    out_dir: str | Path,
    delta: float = 0.1,
    price: float = 1.0,
    use_sqlite: bool = False,
    session_len: int = 0,
    max_turns: int = 0,
) -> RunResult:
    policy = policy or Policy()
    policy.validate()
    counter = make_counter(policy)
    mode = "exact" if counter.mode == CounterMode.EXACT else "estimate"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    db = out / "lattice.db"
    # 必须连 -wal / -shm 一起删：WAL 模式下只删主库，残留的 -wal 会在下次打开时
    # 被当作未 checkpoint 的日志恢复回来 —— 于是新一轮继承了上一轮的 plan 与轮次，
    # 测出来的 B 臂比真实值小（实测污染过一次：21.9% 被抬到 36.3%）。
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            p.unlink()
    arm_a, arm_b, store = _make_arms(transcript, policy, counter,
                                     use_sqlite=use_sqlite, db_path=db)

    rows_a: list[TurnCost] = []
    rows_b: list[TurnCost] = []
    prev_a = prev_b = None

    try:
        for turn in (transcript.turns[:max_turns] if max_turns else transcript.turns):
            # 跨会话：每个会话首轮缓存必定冷启动（上一会话的前缀在本会话不可复用）
            if session_len and turn.index > 1 and (turn.index - 1) % session_len == 0:
                prev_a = prev_b = None
            # 本轮只带 user_text，回复在测量之后再 observe —— 否则 R 段会包含"未来"
            bare = turn.__class__(index=turn.index, user_text=turn.user_text, pinned=turn.pinned)
            step_a = arm_a.step(bare)
            step_b = arm_b.step(bare)

            rows_a.append(metrics.measure_turn(
                arm="A", turn=turn.index, payload=step_a.payload, prev=prev_a,
                counter=counter, delta=delta, price=price,
                boundary=step_a.boundary, degraded=step_a.degraded))
            rows_b.append(metrics.measure_turn(
                arm="B", turn=turn.index, payload=step_b.payload, prev=prev_b,
                counter=counter, delta=delta, price=price,
                boundary=step_b.boundary, degraded=step_b.degraded))
            prev_a, prev_b = step_a.payload, step_b.payload

            for msg in turn.replies:
                arm_a.observe(msg.text, role=msg.role)
                arm_b.observe(msg.text, role=msg.role)

            lines = transcript.attrib_for(turn.index)
            if lines:
                # 归因只影响**下一个边界**的 F，不影响本轮 payload（I3）
                arm_b.session.attribute(list(lines))
    finally:
        arm_a.close()
        arm_b.close()
        if use_sqlite:
            try:
                store.close()
            except Exception:
                pass

    summary = metrics.summarize(
        rows_a=rows_a, rows_b=rows_b, delta=delta, price=price,
        tokenizer=counter.counter_id, transcript_sha256=transcript.sha256, mode=mode)

    by_delta = {
        d: metrics.resummarize(rows_a, rows_b, delta=d, price=price,
                               tokenizer=counter.counter_id,
                               transcript_sha256=transcript.sha256, mode=mode)
        for d in DELTA_GRID
    }

    bstats = metrics.boundary_stats(rows_b)
    _write_csv(out / "turns.csv", rows_a, rows_b)
    (out / "summary.json").write_text(
        json.dumps(summary.to_json_obj(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (out / "report.md").write_text(
        _render_report(transcript, summary, by_delta, bstats, rows_a, rows_b, policy, counter, mode),
        encoding="utf-8")

    return RunResult(
        transcript=transcript.name, locality=transcript.locality, summary=summary,
        summary_by_delta=by_delta, boundary=bstats,
        rows_a=tuple(rows_a), rows_b=tuple(rows_b), out_dir=out, mode=mode)


def _write_csv(path: Path, rows_a: Sequence[TurnCost], rows_b: Sequence[TurnCost]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["arm", "turn", "gross", "cached", "billable", "cost_eq", "boundary", "degraded"])
        for r in list(rows_a) + list(rows_b):
            w.writerow([r.arm, r.turn, r.gross_input, r.cached, r.billable,
                        "%.4f" % r.cost_eq, int(r.boundary), int(r.degraded)])


def _fmt_pct(x: float) -> str:
    return "%+.1f%%" % x


def _render_report(transcript, summary, by_delta, bstats, rows_a, rows_b,
                   policy: Policy, counter, mode: str) -> str:
    n = len(rows_a)
    ga = sum(r.gross_input for r in rows_a)
    gb = sum(r.gross_input for r in rows_b)
    be = summary.break_even_turn

    lines = []
    A = lines.append
    A("# 测量报告 · %s" % transcript.name)
    A("")
    if mode != "exact":
        # 口径必须印在数字**前面**。MEASUREMENT-SPEC 只要求"写进报告"，
        # 但写在第五节的口径块里等于没说 —— 谁会先读末尾再读表格。
        A("> ⚠️ **`mode=%s`：本报告没有精确分词器，token 数是估算的。**" % mode)
        A(">")
        A("> 具体地：`cached`（缓存命中长度）由「字符公共前缀 / chars_per_token」折算，")
        A("> 不是逐 token 比对。**δ 敏感性表在这种口径下不可用于对外结论** ——")
        A("> 它衡量的是缓存定价的影响，而缓存命中的精度正是被估算掉的那个量。")
        A("> 要出对外数字，请装 `tiktoken` 后按 `mode=exact` 重跑。")
        A("")
    A("> 语料与剧本来源见文末「口径」。**本报告不含质量/保真度结论** —— 未做保真核对，")
    A("> 因此不得声称「记得一样多」，只能声称「发出的输入更少」。")
    A("")
    A("## 一、结论")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---:|")
    A("| 输入 token 降幅（累计） | **%s** |" % _fmt_pct(summary.input_saving_pct))
    A("| 等效成本降幅（δ=%.2f） | **%s** |" % (summary.delta_used, _fmt_pct(summary.cost_saving_pct)))
    A("| 回本点（累计净收益首次为正） | %s |" % ("第 %d 轮" % be if be else "**未回本**"))
    A("| 轮数 | %d |" % n)
    A("| A 臂累计输入 | %s token |" % f"{ga:,}")
    A("| B 臂累计输入 | %s token |" % f"{gb:,}")
    A("")
    A("**两个数必须分开读。** 输入降幅是「少发了多少」，成本降幅是「少花了多少」。")
    A("两者不等价，差额全部来自缓存定价 —— 见下面的敏感性表。")
    A("")
    A("## 二、缓存敏感性（δ = 缓存单价 / 全价）")
    A("")
    A("| δ | 含义 | 成本降幅 |")
    A("|---:|---|---:|")
    meaning = {0.1: "缓存九折优惠（主流 provider 常见量级）",
               0.25: "缓存七五折",
               0.5: "缓存半价",
               1.0: "缓存完全不计价（最悲观）"}
    for d in DELTA_GRID:
        A("| %.2f | %s | %s |" % (d, meaning[d], _fmt_pct(by_delta[d].cost_saving_pct)))
    A("")
    A("> **δ=1.0 这一档是方案稳健性的下限**：它回答「即使缓存完全不省钱，本方案还省不省」。")
    A("> 若这一档转负，说明收益完全依赖缓存折扣，那是一条脆弱的结论。")
    A("")
    A("## 三、压缩边界记账（不许摊平）")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---:|")
    A("| 边界轮数 | %d |" % bstats["boundary_count"])
    A("| 边界轮占比 | %.1f%% |" % (bstats["boundary_share"] * 100))
    A("| 边界轮 billable 合计 | %s token |" % f"{bstats['boundary_billable']:,}")
    A("| 边界轮占总 billable | %.1f%% |" % (bstats["boundary_cost_share"] * 100))
    A("| 边界所在轮次 | %s |" % (", ".join(str(t) for t in bstats["boundary_turns"]) or "无"))
    A("")
    A("边界轮前缀被重写、缓存必然失效，是方案**已知且已计入**的成本。")
    A("")
    A("## 四、逐轮明细（每 5 轮抽样，全量见 turns.csv）")
    A("")
    A("| 轮 | A gross | A cached | B gross | B cached | B 边界 |")
    A("|---:|---:|---:|---:|---:|:---:|")
    by_b = {r.turn: r for r in rows_b}
    for ra in rows_a:
        if ra.turn % 5 and ra.turn != n:
            continue
        rb = by_b[ra.turn]
        A("| %d | %s | %s | %s | %s | %s |" % (
            ra.turn, f"{ra.gross_input:,}", f"{ra.cached:,}",
            f"{rb.gross_input:,}", f"{rb.cached:,}", "是" if rb.boundary else ""))
    A("")
    A("## 五、口径（任何人可据此复算）")
    A("")
    A("```")
    A("tokenizer      = %s" % counter.counter_id)
    A("mode           = %s" % mode)
    A("transcript     = %s" % transcript.sha256)
    A("delta (δ)      = %.3f" % summary.delta_used)
    A("price (p)      = %.3f" % summary.price_used)
    A("budget_tokens  = %d" % policy.budget_tokens)
    A("compact_interval (N) = %d / verbatim_keep (K) = %d" % (
        policy.compact_interval, policy.verbatim_keep))
    A("high_water     = %.2f" % policy.high_water)
    A("```")
    A("")
    A("计费三层：`gross = token(payload)`，`cached = 与上一轮逐 token 相同的前缀长度`，")
    A("`billable = gross − cached`，`cost_eq = billable·p + cached·p·δ`。")
    A("")
    A("**语料 / 剧本 / 结果 三者的来源必须分开看：**")
    A("")
    A("| 项 | 取值 |")
    A("|---|---|")
    A("| 语料 | %s |" % (transcript.meta.get("description") or "见 meta.json"))
    A("| 语料来源 | %s |" % (transcript.meta.get("corpus_note") or "真实文件"))
    A("| 剧本来源 | %s |" % (transcript.meta.get("script_source") or "未知"))
    A("| 剧本依据 | %s |" % (transcript.meta.get("script_basis") or "未说明"))
    A("| 结果 | 离线回放（确定性 mock 回复），**不是生产实测** |")
    A("")
    A("## 六、这份报告不能声称什么")
    A("")
    A("- 不能声称「质量打平」—— 没做保真核对。")
    A("- 不能声称「生产环境实测」—— 回复是回放的，不是模型生成的。")
    A("- 不能把「输入降幅」当「省钱」直接对外说 —— 必须带 δ 与成本降幅。")
    A("")
    if transcript.locality == "none":
        A("> **注意：本档是 `locality=none`（短会话）。** B 臂更贵是**预期结果**，"
          "不是缺陷 —— 冻结块在短会话里没有回本空间。若它显示 B 臂反而省，"
          "说明冻结块成本没被计入测量。")
        A("")
    if transcript.locality == "bad":
        A("> **注意：本档是 `locality=bad`（工作集接近全量）。** 这是测量方法的自检档："
          "若 B 臂仍然大省，说明测量有偏，应先修方法再谈结论。")
        A("")
    return "\n".join(lines) + "\n"


def run_all(*, transcript_dir: str | Path, out_root: str | Path, label: str,
            policy: Policy | None = None, delta: float = 0.1, price: float = 1.0,
            use_sqlite: bool = False, session_len: int = 0,
            max_turns: int = 0, only: str = "") -> list[RunResult]:
    tdir = Path(transcript_dir)
    results: list[RunResult] = []
    for jsonl in sorted(tdir.glob("*.jsonl")):
        if jsonl.name.endswith(".attrib.jsonl"):
            continue
        if only and only not in jsonl.name:
            continue
        tr = load_transcript(jsonl)
        results.append(run(tr, policy=policy, out_dir=Path(out_root) / tr.name,
                           delta=delta, price=price, use_sqlite=use_sqlite,
                           session_len=session_len, max_turns=max_turns))
    (Path(out_root) / "CROSS.md").write_text(_render_cross(results), encoding="utf-8")
    return results


def _render_cross(results: Sequence[RunResult]) -> str:
    lines = ["# 跨档汇总", "",
             "三档必须一起读。**只报 `good` 档等于挑选有利场景。**", "",
             "| transcript | locality | 轮数 | 输入降幅 | 成本降幅 (δ=0.1) | 成本降幅 (δ=1.0) | 回本点 |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for r in results:
        be = r.summary.break_even_turn
        lines.append("| %s | %s | %d | %s | %s | %s | %s |" % (
            r.transcript, r.locality, len(r.rows_a),
            _fmt_pct(r.summary.input_saving_pct),
            _fmt_pct(r.summary.cost_saving_pct),
            _fmt_pct(r.summary_by_delta[1.0].cost_saving_pct),
            ("第 %d 轮" % be) if be else "未回本"))
    lines += ["", "## 自检判据", ""]
    good = [r for r in results if r.locality == "good"]
    bad = [r for r in results if r.locality == "bad"]
    none = [r for r in results if r.locality == "none"]
    for grp, name, expect in (
        (good, "good", "应明显为正"),
        (bad, "bad", "应衰减甚至转负；若仍大省 → 测量方法有偏"),
        (none, "none", "**应为负**（B 臂更贵）；若为正 → 冻结块成本没算进去"),
    ):
        for r in grp:
            ok = r.summary.cost_saving_pct > 0 if name == "good" else True
            lines.append("- `%s`（%s）：成本降幅 %s —— 期望%s" % (
                r.transcript, name, _fmt_pct(r.summary.cost_saving_pct), expect))
    lines.append("")
    return "\n".join(lines) + "\n"
