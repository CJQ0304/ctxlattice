"""时钟端口。

**时钟不参与装配**（A3 / §10.1）。它只出现在三处，且三处都不进 payload 字节：

1. 事件记录的时间戳（``CompactionEvent`` / ``DegradationRecord`` 的 ``at_tick``）；
2. 存储行的 ``created_at`` / ``updated_at``（ISO-8601 UTC TEXT，SCHEMA §9.7）；
3. ``SessionStats``。

因此时钟异常在**物理上**无法影响装配结果 —— 这是把「不该耦合的东西」从依赖图
里删掉的收益，不是靠运行期 try/except 兜出来的。``assembler.py`` 连本模块都
不 import，这条约束是可执行的结构事实（可用 import 图断言检查）。

依赖：``errors`` + 标准库。不 import 任何内部模块。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from .errors import ClockAnomalyError

# 逻辑时钟的 ISO 基准点：固定常量，因此 LogicalClock 的 now_iso() 完全可复现。
# 选它只是为了让日志看起来像时间，**它不对应任何真实时刻**。
LOGICAL_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@runtime_checkable
class ClockPort(Protocol):
    """时间源。两个方法都必须单调、可重复调用而不改变语义。"""

    def now_tick(self) -> int:
        """单调递增的整数 tick（毫秒或逻辑步进）。**不得回退。**"""
        ...

    def now_iso(self) -> str:
        """ISO-8601 UTC，形如 ``2026-10-28T09:00:00.000Z``。

        字典序即时间序，可跨库直接比，人工可读（SCHEMA §9.7）。
        禁止与 epoch 整数混用：混用的结果是排序看着对、``BETWEEN`` 静默少一半。
        """
        ...


def format_iso(dt: datetime) -> str:
    """统一到毫秒精度 + ``Z`` 后缀。超出范围的输入裁到毫秒（不四舍五入）。"""
    if dt.tzinfo is None:
        raise ClockAnomalyError(
            "时钟返回了 naive datetime（缺时区）：%r；"
            "请统一用 timezone.utc，否则同一时刻在不同机器上会写出不同字符串" % (dt,))
    utc = dt.astimezone(timezone.utc)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second,
        utc.microsecond // 1000,
    )


class SystemClock:
    """墙钟 + 单调 tick。

    tick 用 ``time.monotonic_ns()`` 而不是 ``time.time()``：墙钟会被 NTP 往回拨，
    而「回退」正是我们要检测的异常。检测到回退 → ``ClockAnomalyError``；
    ``Session`` 收到后切 ``LogicalClock``（F5，对 payload 零影响）。
    """

    def __init__(self) -> None:
        self._last_tick: int | None = None

    def now_tick(self) -> int:
        tick = time.monotonic_ns() // 1_000_000
        if self._last_tick is not None and tick < self._last_tick:
            raise ClockAnomalyError(
                "单调时钟回退：上一 tick %d，本次 %d（相差 %d ms）；"
                "这种情况不应出现在 monotonic 上，请检查宿主是否替换了时钟实现"
                % (self._last_tick, tick, self._last_tick - tick))
        self._last_tick = tick
        return tick

    def now_iso(self) -> str:
        return format_iso(datetime.now(timezone.utc))


class LogicalClock:
    """纯逻辑时钟：``tick`` 从 0 起，每次 ``advance()`` +1。

    完全可复现（同操作序列 → 同 tick 序列 → 同 ISO 字符串），因此 bench 的离线
    回放用它。它是 F5 的兜底：墙钟不可用时切到它，payload 字节不受影响。
    """

    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ClockAnomalyError("LogicalClock 的起始 tick 必须 ≥ 0，实测 %d" % start)
        self._tick = start

    def now_tick(self) -> int:
        return self._tick

    def advance(self, step: int = 1) -> int:
        if step < 0:
            raise ClockAnomalyError("LogicalClock.advance 的步长不得为负：%d" % step)
        self._tick += step
        return self._tick

    def now_iso(self) -> str:
        return format_iso(LOGICAL_EPOCH + timedelta(seconds=self._tick))


class FrozenClock:
    """固定时刻的时钟。给「必须完全确定」的测试与存储写入用。"""

    def __init__(self, iso: str = "2026-01-01T00:00:00.000Z", tick: int = 0) -> None:
        self._iso = iso
        self._tick = tick

    def now_tick(self) -> int:
        return self._tick

    def now_iso(self) -> str:
        return self._iso


def safe_tick(clock: ClockPort | None) -> int:
    """取 tick，异常时退回 0。**仅供事件记录与日志用。**

    装配路径不调用它（装配器不 import 本模块），所以这里的「吞异常」
    不可能掩盖一个会改变 payload 的故障。
    """
    if clock is None:
        return 0
    try:
        return int(clock.now_tick())
    except Exception:  # pragma: no cover - 只在时钟坏掉时走到
        return 0


def safe_iso(clock: ClockPort | None, fallback: str = "1970-01-01T00:00:00.000Z") -> str:
    if clock is None:
        return fallback
    try:
        return clock.now_iso()
    except Exception:  # pragma: no cover
        return fallback


__all__ = [
    "ClockPort",
    "FrozenClock",
    "LOGICAL_EPOCH",
    "LogicalClock",
    "SystemClock",
    "format_iso",
    "safe_iso",
    "safe_tick",
]
