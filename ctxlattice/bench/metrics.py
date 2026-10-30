"""计费三层口径（MEASUREMENT-SPEC §3）。

一句话：**少发不等于省钱**。必须同时给出 gross 降幅与扣除缓存折扣后的成本降幅。

本模块不读存储、不碰时钟、不做 IO —— 只做算术，保证同一批 payload 得到同一批数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from ..model import Payload
    from ..tokens import TokenCounter

#: 敏感性档位。δ = 1.0 表示"缓存完全不计价"，是方案稳健性的下限。
DELTA_GRID = (0.1, 0.25, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class TurnCost:
    arm: str
    turn: int
    gross_input: int
    cached: int
    billable: int
    cost_eq: float
    boundary: bool
    degraded: bool


@dataclass(frozen=True, slots=True)
class Summary:
    input_saving_pct: float
    cost_saving_pct: float
    break_even_turn: int | None
    delta_used: float
    price_used: float
    tokenizer: str
    transcript_sha256: str
    mode: str

    def to_json_obj(self) -> dict:
        """字段名固定，不得增删（SPEC §7：改字段名 = 改口径）。"""
        return {
            "input_saving_pct": self.input_saving_pct,
            "cost_saving_pct": self.cost_saving_pct,
            "break_even_turn": self.break_even_turn,
            "delta_used": self.delta_used,
            "price_used": self.price_used,
            "tokenizer": self.tokenizer,
            "transcript_sha256": self.transcript_sha256,
            "mode": self.mode,
        }


def cache_prefix_tokens(prev: "Payload | None", cur: "Payload", counter: "TokenCounter") -> int:
    """与上一轮 payload 逐 token 相同的公共前缀长度。

    这是整个测量里最容易糊弄的一个量，所以写死两条：
    * **逐 token 比对**，不是按字符或按行；
    * 没有精确分词器时退化为「**字符**公共前缀 / chars_per_token」，
      并由调用方把 ``mode=estimate`` 写进报告 —— 不许悄悄当成精确值。

    为什么退化路径必须是"字符"而不是"字节"：它是 ``count()`` 的同一个单位。
    ``EstimateCounter.count()`` 数的是 ``ceil(len(text)/cpt)``，其中 ``len`` 是
    **字符数**（中文 1 字算 1）。改成数字节再除 cpt，等于把中文前缀放大到 3 倍
    （UTF-8 中文 3 字节/字），``cached`` 被 ``min(·, gross)`` 一截就变成
    ``cached == gross``、``billable == 0`` —— 于是 δ 敏感性表塌成
    「成本降幅 == 输入降幅」，δ=0.1 那一档会从负数翻成正数。
    **这是个让结论变漂亮因而不会报错的错**，已实测踩中一次。
    """
    if prev is None:
        return 0
    a, b = prev.text, cur.text
    try:
        ta, tb = counter.encode(a), counter.encode(b)
    except (AttributeError, NotImplementedError):
        # 估算器口径：与 ``count()`` 同单位（字符），同取整方式（ceil）。
        cpt = getattr(counter, "chars_per_token", 2) or 2
        chars = 0
        for x, y in zip(a, b):
            if x != y:
                break
            chars += 1
        return int(math.ceil(chars / float(cpt)))
    n = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        n += 1
    return n


def measure_turn(
    *,
    arm: str,
    turn: int,
    payload: "Payload",
    prev: "Payload | None",
    counter: "TokenCounter",
    delta: float,
    price: float,
    boundary: bool,
    degraded: bool = False,
) -> TurnCost:
    gross = payload.token_count
    cached = min(cache_prefix_tokens(prev, payload, counter), gross)
    billable = gross - cached
    return TurnCost(
        arm=arm,
        turn=turn,
        gross_input=gross,
        cached=cached,
        billable=billable,
        cost_eq=billable * price + cached * price * delta,
        boundary=boundary,
        degraded=degraded,
    )


def _totals(rows: Sequence[TurnCost]) -> tuple[int, float]:
    return sum(r.gross_input for r in rows), sum(r.cost_eq for r in rows)


def break_even_turn(rows_a: Sequence[TurnCost], rows_b: Sequence[TurnCost]) -> int | None:
    """累计净收益首次为正的轮次；跑完都没回本 → ``None``（照实写 null，不用"趋势"替代）。"""
    by_a = {r.turn: r for r in rows_a}
    cum = 0.0
    for rb in sorted(rows_b, key=lambda r: r.turn):
        ra = by_a.get(rb.turn)
        if ra is None:
            continue
        cum += ra.cost_eq - rb.cost_eq
        if cum > 0:
            return rb.turn
    return None


def summarize(
    *,
    rows_a: Sequence[TurnCost],
    rows_b: Sequence[TurnCost],
    delta: float,
    price: float,
    tokenizer: str,
    transcript_sha256: str,
    mode: str,
) -> Summary:
    ga, ca = _totals(rows_a)
    gb, cb = _totals(rows_b)
    return Summary(
        input_saving_pct=0.0 if ga == 0 else (1.0 - gb / ga) * 100.0,
        cost_saving_pct=0.0 if ca == 0 else (1.0 - cb / ca) * 100.0,
        break_even_turn=break_even_turn(rows_a, rows_b),
        delta_used=delta,
        price_used=price,
        tokenizer=tokenizer,
        transcript_sha256=transcript_sha256,
        mode=mode,
    )


def resummarize(rows_a: Sequence[TurnCost], rows_b: Sequence[TurnCost], *,
                delta: float, price: float, tokenizer: str,
                transcript_sha256: str, mode: str) -> Summary:
    """换一组 δ/单价重算成本降幅（其余量不变）。敏感性表用它，不重跑装配。"""
    def recost(rows: Sequence[TurnCost]) -> float:
        return sum(r.billable * price + r.cached * price * delta for r in rows)

    ga, _ = _totals(rows_a)
    gb, _ = _totals(rows_b)
    ca = recost(rows_a)
    cb = recost(rows_b)
    return Summary(
        input_saving_pct=0.0 if ga == 0 else (1.0 - gb / ga) * 100.0,
        cost_saving_pct=0.0 if ca == 0 else (1.0 - cb / ca) * 100.0,
        break_even_turn=break_even_turn(rows_a, rows_b),
        delta_used=delta,
        price_used=price,
        tokenizer=tokenizer,
        transcript_sha256=transcript_sha256,
        mode=mode,
    )


def boundary_stats(rows: Sequence[TurnCost]) -> dict:
    """边界轮的显式记账。**不许写成「≈0 忽略不计」**（ARCHITECTURE §9.2）。"""
    total = len(rows)
    bounds = [r for r in rows if r.boundary]
    billable_all = sum(r.billable for r in rows)
    billable_b = sum(r.billable for r in bounds)
    return {
        "boundary_count": len(bounds),
        "boundary_share": (len(bounds) / total) if total else 0.0,
        "boundary_billable": billable_b,
        "boundary_cost_share": (billable_b / billable_all) if billable_all else 0.0,
        "boundary_turns": [r.turn for r in bounds],
    }
