"""策略参数 + 校验 + 边界判定。

两条贯穿全文件的纪律：

1. **边界以「轮」计，不以「时间」计**。``compact_interval`` 是轮数，时钟完全不
   进入装配路径（A3）。这样时钟异常在物理上无法影响 payload 字节（§10.1）。
2. ``digest()`` 只包含**会影响 payload 字节**的字段。它进 ``AssemblyPlan``，
   用于事后回答「这份 payload 是哪个参数组合产生的」（§6.2）。
   ``strict_invariants`` 这类不改变字节的开关**故意不进 digest** —— 进了会让
   两次参数等价、字节相同的运行报出不同的 digest，等于自己破坏可复现性。

依赖：``model``（类型）、``errors``。**不 import 存储 / 时钟 / 渲染。**
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .errors import PolicyError

if TYPE_CHECKING:
    from .model import AssemblyPlan


class BoundaryReason(str, Enum):
    COLD_START = "cold_start"      # 第 1 轮：无 plan，必须建
    INTERVAL = "interval"          # 达到 N 轮
    PRESSURE = "pressure"          # 预估 token > budget * high_water
    FORCED = "forced"              # 调用方 force_compact()
    PLAN_INVALID = "plan_invalid"  # plan 缺失 / 校验不符 / 冻结块不可读
    NONE = "none"                  # 非边界


class DegradeReason(str, Enum):
    STORE_CORRUPT = "store_corrupt"
    STORE_IO = "store_io"
    DISK_FULL = "disk_full"
    BUDGET_OVERFLOW = "budget_overflow"
    CLOCK_ANOMALY = "clock_anomaly"
    ENCODING_ERROR = "encoding_error"
    PLAN_REBUILT = "plan_rebuilt"
    EMPTY_STORE_NO_COMPACT = "empty_store_no_compact"
    MISSING_ASSISTANT = "missing_assistant"
    FREEZE_ABORT = "freeze_abort"
    #: P9 被违反（同一 session_id 两个驱动）。**不是**存储损坏，见 F16。
    #: 单独一个原因值是为了让报告能把"去改进程模型"和"去查库"分开 ——
    #: 这两件事的处置方向相反，混成一个值等于把诊断责任推给调用方。
    CONCURRENT_DRIVER = "concurrent_driver"
    INTERNAL = "internal"


#: **不可逆**的降级原因：一旦成立，此后每一轮都仍然成立，除非人为干预。
#:
#: 判据只有一条 —— **「下一轮还会不会成立」**，不是「这条原因是谁记下来的」。
#: 按来源（模式类 / 事件类）分类会随新增原因不断重新归类，按可逆性分类不用。
#:
#: 为什么这件事重要：payload 上只有一个 ``degrade_reason`` 字段，而一轮里可能有
#: 两条原因（库已坏 + 本轮回复没登记）。可逆的那条下一轮自己就消失了，不可逆的
#: 会一直在 —— 被它盖住等于**最该修的那条从报告里消失**，而所有断言仍然是绿的。
#:
#: 这四条都对应一个已锁存的状态位：``_readable=False`` / ``_writable=False`` /
#: ``_writes_halted=True``。``INTERNAL`` **不在**里面：它多数是一次性的意外，
#: 把它当不可逆会让任何一次抖动永久占据报告。
IRREVERSIBLE_REASONS: frozenset[str] = frozenset({
    DegradeReason.STORE_CORRUPT.value,
    DegradeReason.STORE_IO.value,
    DegradeReason.DISK_FULL.value,
    DegradeReason.CONCURRENT_DRIVER.value,
})


def pick_reason(
    first: "DegradeReason | str | None",
    second: "DegradeReason | str | None",
) -> "DegradeReason | str | None":
    """在两条候选原因里选**更该被看见**的那条。返回 ``str`` 还是枚举取决于入参。

    规则两条：

    1. **不可逆的胜过可逆的**（见 ``IRREVERSIBLE_REASONS``）；
    2. 同层（都可逆 / 都可逆）时**保留第一个** —— 第一个是更早的观测，且这条
       与各处 ``or`` 的既有语义一致，不会因为引入优先级而改变既有行为。

    只做选择，不记事件：事件由 ``_record()`` 在检测到的那一刻记一次。
    """
    if first is None:
        return second
    if second is None:
        return first
    a = first.value if isinstance(first, DegradeReason) else first
    b = second.value if isinstance(second, DegradeReason) else second
    if (b in IRREVERSIBLE_REASONS) and (a not in IRREVERSIBLE_REASONS):
        return second
    return first


_ON_MISSING_ASSISTANT = ("mark", "skip")
_ON_EMPTY_STORE = ("no_compact", "degrade_history")
_MANDATORY_OVERFLOW = ("warn", "truncate")
_COUNTER_PREFER = ("exact", "estimate")


@dataclass(frozen=True, slots=True)
class Policy:
    # —— 预算与边界 ——
    budget_tokens: int = 8000
    high_water: float = 0.75
    compact_interval: int = 12                 # N
    verbatim_keep: int = 6                     # K（**段起点**，不是滑动窗口宽度）
    # —— 冻结块配比 ——
    f_share: float = 0.35
    u_reserve_tokens: int = 512
    f_hard_cap_ratio: float = 1.5
    # —— 相关子树选取 ——
    relevance_ngram: int = 2
    relevance_floor: int = 2
    recency_window: int = 6
    max_topics_in_f: int = 8
    max_purposes_per_topic: int = 6
    # —— 降级与放行 ——
    on_missing_assistant: str = "mark"          # mark | skip
    on_empty_store: str = "no_compact"          # no_compact | degrade_history
    mandatory_overflow: str = "warn"            # warn | truncate
    strict_invariants: bool = False             # True → 破坏前缀稳定性时抛
    # —— 计数口径 ——
    counter_prefer: str = "exact"               # exact | estimate
    tiktoken_encoding: str = "o200k_base"

    # -- 校验 -------------------------------------------------------------
    def validate(self) -> None:
        """非法则抛 ``PolicyError``。**构造期不 fail-open**（A4 / F15）。"""
        if self.budget_tokens < 1:
            raise PolicyError("policy.budget_tokens 必须 ≥ 1，实测 %d" % self.budget_tokens)
        if not (0.0 < self.high_water < 1.0):
            raise PolicyError(
                "policy.high_water 必须落在开区间 (0, 1)，实测 %r"
                "（取 0.75 表示用到 75%% 预算就提前压缩）" % (self.high_water,))
        if self.compact_interval < 1:
            raise PolicyError("policy.compact_interval 必须 ≥ 1，实测 %d" % self.compact_interval)
        if self.verbatim_keep < 1:
            raise PolicyError("policy.verbatim_keep 必须 ≥ 1，实测 %d" % self.verbatim_keep)
        if self.verbatim_keep > self.compact_interval:
            raise PolicyError(
                "policy.verbatim_keep (K=%d) 不得大于 compact_interval (N=%d)："
                "K 是边界时的 R 段起点偏移，K > N 会让 R 段在边界上反而左移，"
                "前缀稳定性（I1）无从成立" % (self.verbatim_keep, self.compact_interval))
        if not (0.0 < self.f_share <= 1.0):
            raise PolicyError("policy.f_share 必须落在 (0, 1]，实测 %r" % (self.f_share,))
        if self.u_reserve_tokens < 0:
            raise PolicyError("policy.u_reserve_tokens 必须 ≥ 0，实测 %d" % self.u_reserve_tokens)
        if self.f_hard_cap_ratio < 1.0:
            raise PolicyError(
                "policy.f_hard_cap_ratio 必须 ≥ 1.0，实测 %r"
                "（它是软预算的倍数上限，小于 1 意味着硬上限比软预算还紧）"
                % (self.f_hard_cap_ratio,))
        if self.relevance_ngram < 1:
            raise PolicyError("policy.relevance_ngram 必须 ≥ 1，实测 %d" % self.relevance_ngram)
        if self.relevance_floor < 0:
            raise PolicyError("policy.relevance_floor 必须 ≥ 0，实测 %d" % self.relevance_floor)
        if self.recency_window < 0:
            raise PolicyError("policy.recency_window 必须 ≥ 0，实测 %d" % self.recency_window)
        if self.max_topics_in_f < 1:
            raise PolicyError("policy.max_topics_in_f 必须 ≥ 1，实测 %d" % self.max_topics_in_f)
        if self.max_purposes_per_topic < 1:
            raise PolicyError(
                "policy.max_purposes_per_topic 必须 ≥ 1，实测 %d" % self.max_purposes_per_topic)
        if self.on_missing_assistant not in _ON_MISSING_ASSISTANT:
            raise PolicyError("policy.on_missing_assistant 只能是 %s，实测 %r"
                              % (" / ".join(_ON_MISSING_ASSISTANT), self.on_missing_assistant))
        if self.on_empty_store not in _ON_EMPTY_STORE:
            raise PolicyError("policy.on_empty_store 只能是 %s，实测 %r"
                              % (" / ".join(_ON_EMPTY_STORE), self.on_empty_store))
        if self.mandatory_overflow not in _MANDATORY_OVERFLOW:
            raise PolicyError("policy.mandatory_overflow 只能是 %s，实测 %r"
                              % (" / ".join(_MANDATORY_OVERFLOW), self.mandatory_overflow))
        if self.counter_prefer not in _COUNTER_PREFER:
            raise PolicyError("policy.counter_prefer 只能是 %s，实测 %r"
                              % (" / ".join(_COUNTER_PREFER), self.counter_prefer))
        if not self.tiktoken_encoding:
            raise PolicyError("policy.tiktoken_encoding 不能为空")
        # 交叉约束：F 的软预算 + U 预留 不得超过总预算，否则 U 永远无处安放。
        if self.f_soft_budget() + self.u_reserve_tokens > self.budget_tokens:
            raise PolicyError(
                "policy.f_share (%r) × budget_tokens (%d) = %d，加上 u_reserve_tokens (%d) "
                "已超过 budget_tokens (%d)：F 会把预算吃满，本轮用户消息无处安放"
                % (self.f_share, self.budget_tokens, self.f_soft_budget(),
                   self.u_reserve_tokens, self.budget_tokens))

    # -- 派生额度 ---------------------------------------------------------
    def f_soft_budget(self) -> int:
        """F 的软预算：``int(budget * f_share)``。全整数，无浮点残留。"""
        return int(self.budget_tokens * self.f_share)

    def f_hard_cap(self) -> int:
        """F 的硬上限：``int(f_soft * f_hard_cap_ratio)``。超过才真截。"""
        return int(self.f_soft_budget() * self.f_hard_cap_ratio)

    def r_budget(self, s_tokens: int, f_tokens: int) -> int:
        """R 段可用额度 = 预算 − S − F − U 预留，下限 0。"""
        return max(0, self.budget_tokens - s_tokens - f_tokens - self.u_reserve_tokens)

    def pressure_threshold(self) -> int:
        return int(self.budget_tokens * self.high_water)

    # -- digest -----------------------------------------------------------
    def digest(self) -> str:
        """影响 payload 字节的参数摘要（sha256 前 16 位）。

        字段顺序写死在这里，不依赖 ``dataclasses.fields()`` 的声明顺序 ——
        否则将来「把两个字段换个位置」这种看起来无害的重构会静默改变所有
        历史 payload 的 digest。
        """
        parts = [
            "policy1",
            "budget=%d" % self.budget_tokens,
            "high_water=%s" % _fmt_float(self.high_water),
            "N=%d" % self.compact_interval,
            "K=%d" % self.verbatim_keep,
            "f_share=%s" % _fmt_float(self.f_share),
            "u_reserve=%d" % self.u_reserve_tokens,
            "f_hard_ratio=%s" % _fmt_float(self.f_hard_cap_ratio),
            "rel_ngram=%d" % self.relevance_ngram,
            "rel_floor=%d" % self.relevance_floor,
            "recency=%d" % self.recency_window,
            "max_topics=%d" % self.max_topics_in_f,
            "max_purposes=%d" % self.max_purposes_per_topic,
            "missing_assistant=%s" % self.on_missing_assistant,
            "empty_store=%s" % self.on_empty_store,
            "mandatory_overflow=%s" % self.mandatory_overflow,
            "counter=%s" % self.counter_prefer,
            "encoding=%s" % self.tiktoken_encoding,
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    # -- 边界判定 ---------------------------------------------------------
    def boundary_due(
        self,
        plan: "AssemblyPlan | None",
        turn_index: int,
        estimated_tokens: int,
    ) -> BoundaryReason:
        """判定顺序固定（§9.1 自上而下），保证同输入同结果。

        ``PLAN_INVALID`` **不在这里判**：它需要比对当前快照的 ``system.sha16`` /
        ``policy.digest()`` / 冻结块正文，而本签名只拿到 ``plan`` 与两个标量。
        由调用方（``Session._decide_boundary``）先判 plan 有效性，无效时直接给
        ``PLAN_INVALID`` 而不是绕成 ``COLD_START`` —— 两者的语义不同
        （前者意味着「曾经有过、现在不能用了」，后者意味着「从未有过」）。
        """
        if plan is None:
            return BoundaryReason.COLD_START
        if turn_index - plan.epoch_turn >= self.compact_interval:
            return BoundaryReason.INTERVAL
        if estimated_tokens > self.pressure_threshold():
            return BoundaryReason.PRESSURE
        return BoundaryReason.NONE


def _fmt_float(value: float) -> str:
    """浮点的规范化形式：``repr`` 给出最短往返表示，跨平台稳定。

    禁用 ``%f``/``format`` 的定点格式化：``%.6f`` 会把 0.7500001 与 0.7500002
    折成同一串，那等于让 digest 对参数变化失明。
    """
    return repr(float(value))


__all__ = [
    "BoundaryReason", "DegradeReason", "IRREVERSIBLE_REASONS", "Policy", "pick_reason",
]
