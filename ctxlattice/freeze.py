"""冻结块构建 —— **只在压缩边界调用**。

F 是「被摘要吞掉就致命」的那批内容的容器。分层顺序（§7.1）就是字节顺序，
不可运行时调整：

    T0 会话总目标 → T1 钉住 → T2 约束 → T3 改口轨迹 → T4 未决
    → T5 骨架 → T6 相关子树 → T7 事件指针

为什么 T1–T4 在前：硬约束被裁 → 后续产出违规；否定条件被吞 → 直接做错；
改口轨迹丢 → 用回已否决的旧方案；未决待办丢 → 要重新问用户。这四类不能省。
为什么 T6 在骨架之后：先给导航（能定位），再给内容（能动手）—— 这是
``read --level 1 → --topic`` 渐进披露在 payload 里的等价物。

**牺牲顺序写死在这里，不临场决定**（§7.3）：预算是有限的，但「有限时先丢什么」
必须是可复现的常量，否则同一份存储在两次运行里会产出不同的 F。

纯函数：同输入 → 同字节。不 import 存储 / 时钟 / 环境。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Sequence

from .model import Purpose, StoreSnapshot, Topic
from .policy import Policy
from .relevance import ScoredNode, select_relevant, select_subtrees
from .render import (
    F_HEADER_FMT,
    render_leaf,
    render_topic_line,
    sha16,
    single_line,
    tier_line,
)
from .text import (
    TAG_CONSTRAINT,
    TAG_FILL,
    TAG_LATEST,
    TAG_PINNED,
    TAG_STALE,
    TAG_TODO,
    check_leaf_kind,
)

if TYPE_CHECKING:
    from .model import AssemblyPlan, SystemPrompt, Turn
    from .tokens import TokenCounter


class FTier(str, Enum):
    T0_GOAL = "T0"          # 会话总目标
    T1_PINNED = "T1"        # [钉住] + transcript pinned:true 的原文行
    T2_CONSTRAINT = "T2"    # [约束]
    T3_REVERSAL = "T3"      # [最新] / [已废] 成对（改口轨迹）
    T4_PENDING = "T4"       # [待办] / [待补]
    T5_SKELETON = "T5"      # T + P 名（导航层）
    T6_RELEVANT = "T6"      # 相关子树的 R 行
    T7_EVENT = "T7"         # E 指针行


#: 渲染顺序 = 字节顺序（T0..T7）
TIER_ORDER: tuple[FTier, ...] = (
    FTier.T0_GOAL, FTier.T1_PINNED, FTier.T2_CONSTRAINT, FTier.T3_REVERSAL,
    FTier.T4_PENDING, FTier.T5_SKELETON, FTier.T6_RELEVANT, FTier.T7_EVENT,
)

#: 牺牲顺序（§7.3）。T6 天然最先（它最后装、只吃剩余额度）。
#: 每一项是「牺牲单元」的前缀，同一前缀下的单元从**最旧**开始整单元丢。
#: 单元是「最小可牺牲粒度」：T2/T4/T5 的一条是一个单元；T3 的**一整个改口组**
#: 是一个单元（只丢一半会得到「有一个 [已废] 但不知道现在是什么」这种更糟的形态）。
SACRIFICE_ORDER: tuple[str, ...] = (
    "T3-group:",  # 改口轨迹：整组（[最新] + 其 [已废]）成对删，从最旧的组开始
    "T5-p:",      # 骨架里的 P 名（保留 T 名）
    "T4:",        # 未决，从最旧开始
    "T2:",        # 约束，从最旧开始 —— 到这一步说明预算严重不足
)

TIER_LABEL: dict[FTier, str] = {
    FTier.T0_GOAL: "目标",
    FTier.T1_PINNED: "钉住",
    FTier.T2_CONSTRAINT: "约束",
    FTier.T3_REVERSAL: "改口",
    FTier.T4_PENDING: "未决",
    FTier.T5_SKELETON: "骨架",
    FTier.T6_RELEVANT: "相关",
    FTier.T7_EVENT: "事件",
}


@dataclass(frozen=True, slots=True)
class TierStat:
    included: int
    omitted: int
    tokens: int


@dataclass(frozen=True, slots=True)
class FrozenBlock:
    text: str
    sha16: str
    token_count: int
    tier_stats: Mapping[FTier, TierStat]
    aborted: bool = False
    abort_reason: str | None = None


# ---------------------------------------------------------------------------
# 内部：可牺牲的条目
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Item:
    tier: FTier
    lines: tuple[str, ...]
    tokens: int
    order: int
    unit: str            # 牺牲单元：见 SACRIFICE_ORDER + "T0"/"T1"/"T5-t"/"T7"/"T3-group:<n>"
    dropped: bool = False

    @property
    def text(self) -> str:
        return "".join(self.lines)


@dataclass(slots=True)
class _Builder:
    counter: "TokenCounter"
    items: list[_Item] = field(default_factory=list)
    _order: int = 0

    def add(
        self, tier: FTier, lines: Sequence[str], unit: str, *, per_item: bool = True
    ) -> _Item:
        """登记一个可牺牲条目。

        ``per_item=True`` → 本条目自成一个牺牲单元（``unit:order``）；
        ``per_item=False`` → 与同 ``unit`` 的条目共用一个单元（用于"成对删"）。

        单元键一律以 ``:`` 分段，因为 ``SACRIFICE_ORDER`` 用前缀匹配定位 sacrificing
        范围（``"T2:"`` 不能匹配到 ``"T2x"`` 这种前缀污染）。
        """
        key = "%s:%d" % (unit, self._order) if per_item else unit
        text = "".join(lines)
        item = _Item(
            tier=tier,
            lines=tuple(lines),
            tokens=self.counter.count(text),
            order=self._order,
            unit=key,
        )
        self._order += 1
        self.items.append(item)
        return item

    def of_tier(self, tier: FTier) -> list[_Item]:
        return [i for i in self.items if i.tier is tier]


def _multi_line(label: str, prefix: str, text: str) -> list[str]:
    """把可能多行的原文渲染成若干行 tier 行。

    逐行加前缀而不是原样塞进去：F 段的行首带 ``[标签]``，若正文里带换行，
    就能伪造出一个假的 ``[约束]`` 行，让「哪些是硬约束」变成不可判定。
    逐行加前缀同时保住原文（每行逐字），只是不再保留换行的原始位置信息。
    """
    parts = text.split("\n")
    out = [tier_line(label, prefix + parts[0])]
    for line in parts[1:]:
        out.append(tier_line(label, "  " + line))
    return out


def _iter_leaves(topics: Sequence[Topic]):
    """按存储顺序产出 ``(topic, purpose, leaf, leaf_path)``。"""
    for topic in topics:
        for purpose in topic.purposes:
            for idx, leaf in enumerate(purpose.leaves):
                kind = check_leaf_kind(leaf.kind)
                yield (topic, purpose, leaf,
                       "T%d/P%s/%s%d" % (topic.num, purpose.num, kind, idx + 1))


# ---------------------------------------------------------------------------
# 各 tier 的候选
# ---------------------------------------------------------------------------
def _collect_goal(snapshot: StoreSnapshot, b: _Builder) -> None:
    if not snapshot.top_text:
        return
    b.add(FTier.T0_GOAL, _multi_line(TIER_LABEL[FTier.T0_GOAL], "", snapshot.top_text), "T0")


def _collect_pinned(snapshot: StoreSnapshot, turn_log: Sequence["Turn"], b: _Builder) -> None:
    label = TIER_LABEL[FTier.T1_PINNED]
    for topic, purpose, leaf, path in _iter_leaves(snapshot.topics):
        if leaf.tag != TAG_PINNED:
            continue
        body = "%s %s" % (path, leaf.text) if leaf.kind == "R" else "%s %s" % (path, render_leaf(leaf))
        b.add(FTier.T1_PINNED, _multi_line(label, "", body), "T1")
    for turn in turn_log:
        if not turn.pinned:
            continue
        b.add(FTier.T1_PINNED,
              _multi_line(label, "第%d轮 " % turn.index, turn.user_text), "T1")


def _collect_constraints(snapshot: StoreSnapshot, b: _Builder) -> None:
    label = TIER_LABEL[FTier.T2_CONSTRAINT]
    for _topic, _purpose, leaf, path in _iter_leaves(snapshot.topics):
        if leaf.tag != TAG_CONSTRAINT:
            continue
        body = "%s %s" % (path, leaf.text) if leaf.kind == "R" else "%s %s" % (path, render_leaf(leaf))
        b.add(FTier.T2_CONSTRAINT, _multi_line(label, "", body), "T2")


def _collect_reversals(snapshot: StoreSnapshot, b: _Builder) -> None:
    """改口轨迹：``[最新]`` 与其配对的 ``[已废]``，**同一目的下成组**。

    配对口径（写死，不临场判断）：同一 ``topic/purpose`` 下的全部 ``[最新]`` 与
    ``[已废]`` 构成一组；组内先出 ``[最新]``（当前有效的那个），再按链序出
    ``[已废]``（被否决的那些）。牺牲时**整组**一起丢，从最旧的组开始 ——
    只丢一半会得到「有一个 [已废] 但不知道现在是什么」这种更糟的形态。
    """
    latest_label = "改口·新"
    stale_label = "改口·旧"
    for group_index, (topic, purpose) in enumerate(
        (t, p) for t in snapshot.topics for p in t.purposes
    ):
        newest = [l for l in purpose.leaves if l.kind == "R" and l.tag == TAG_LATEST]
        stale = [l for l in purpose.leaves if l.kind == "R" and l.tag == TAG_STALE]
        if not newest and not stale:
            continue
        unit = "T3-group:%d" % group_index
        path = "T%d/P%s" % (topic.num, purpose.num)
        for leaf in newest:
            b.add(FTier.T3_REVERSAL,
                  _multi_line(latest_label, "%s " % path, leaf.text), unit, per_item=False)
        for leaf in stale:
            b.add(FTier.T3_REVERSAL,
                  _multi_line(stale_label, "%s " % path, leaf.text), unit, per_item=False)


def _collect_pending(snapshot: StoreSnapshot, b: _Builder) -> None:
    label = TIER_LABEL[FTier.T4_PENDING]
    for topic in snapshot.topics:
        for purpose in topic.purposes:
            path = "T%d/P%s" % (topic.num, purpose.num)
            if purpose.tag == TAG_TODO:
                b.add(FTier.T4_PENDING,
                      _multi_line(label, "%s 待办目的 " % path, purpose.name), "T4")
            for leaf in purpose.leaves:
                if leaf.tag != TAG_FILL or leaf.kind != "R":
                    continue
                b.add(FTier.T4_PENDING,
                      _multi_line(label, "%s 待补 " % path, leaf.text), "T4")


def _collect_skeleton(snapshot: StoreSnapshot, b: _Builder) -> None:
    label = TIER_LABEL[FTier.T5_SKELETON]
    for topic in snapshot.topics:
        b.add(FTier.T5_SKELETON,
              [tier_line(label, render_topic_line(topic))], "T5-t")
        for purpose in topic.purposes:
            b.add(FTier.T5_SKELETON,
                  [tier_line(label, "  P%s %s" % (purpose.num, purpose.name))], "T5-p")


def _collect_events(snapshot: StoreSnapshot, b: _Builder) -> None:
    label = TIER_LABEL[FTier.T7_EVENT]
    for _topic, _purpose, leaf, _path in _iter_leaves(snapshot.topics):
        if leaf.kind != "E":
            continue
        b.add(FTier.T7_EVENT, [tier_line(label, render_leaf(leaf))], "T7")


def _focus_selections(
    snapshot: StoreSnapshot, focus: Sequence[str]
) -> tuple[tuple[Topic, tuple[Purpose, ...]], ...]:
    """把调用方点名的路径解析成子树。未知路径静默忽略（不猜、不报错）。"""
    by_topic = {t.path: t for t in snapshot.topics}
    by_purpose: dict[str, tuple[Topic, Purpose]] = {}
    for t in snapshot.topics:
        for p in t.purposes:
            by_purpose["T%d/P%s" % (t.num, p.num)] = (t, p)

    out: list[tuple[Topic, tuple[Purpose, ...]]] = []
    seen: set[str] = set()
    for raw in focus:
        path = single_line(raw)
        if path in by_topic:
            topic = by_topic[path]
            if topic.path in seen:
                continue
            seen.add(topic.path)
            out.append((topic, topic.purposes))
            continue
        if path in by_purpose:
            topic, purpose = by_purpose[path]
            if topic.path in seen:
                continue
            seen.add(topic.path)
            out.append((topic, (purpose,)))
    return tuple(out)


def _collect_relevant(
    snapshot: StoreSnapshot,
    b: _Builder,
    *,
    query: str,
    turn_index: int,
    policy: Policy,
    focus: Sequence[str],
    last_touch: Mapping[str, int],
) -> list[_Item]:
    label = TIER_LABEL[FTier.T6_RELEVANT]
    nodes: tuple[ScoredNode, ...] = select_relevant(
        query=query, snapshot=snapshot, turn_index=turn_index,
        policy=policy, last_touch=last_touch,
    )

    ordered: list[tuple[Topic, tuple[Purpose, ...]]] = list(_focus_selections(snapshot, focus))
    taken = {t.path for t, _ps in ordered}
    for sel in select_subtrees(nodes=nodes, snapshot=snapshot, policy=policy):
        if sel.topic.path in taken:
            continue
        taken.add(sel.topic.path)
        ordered.append((sel.topic, sel.purposes))

    out: list[_Item] = []
    for topic, purposes in ordered:
        out.append(b.add(FTier.T6_RELEVANT,
                         [tier_line(label, render_topic_line(topic))], "T6"))
        for purpose in purposes:
            out.append(b.add(FTier.T6_RELEVANT,
                             [tier_line(label, "  P%s %s" % (purpose.num, purpose.name))], "T6"))
            for leaf in purpose.leaves:
                out.append(b.add(FTier.T6_RELEVANT,
                                 [tier_line(label, "    " + render_leaf(leaf))], "T6"))
    return out


# ---------------------------------------------------------------------------
# 预算与牺牲
# ---------------------------------------------------------------------------
def _sacrifice(b: _Builder, prefix: str, total: int, limit: int) -> int:
    """按**整单元**丢条目，从最旧（``order`` 最小）的单元开始，直到 ``total <= limit``。"""
    units: dict[str, list[_Item]] = {}
    for item in b.items:
        if item.dropped or not item.unit.startswith(prefix):
            continue
        units.setdefault(item.unit, []).append(item)
    ordered = sorted(units.values(), key=lambda items: min(i.order for i in items))
    for items in ordered:
        if total <= limit:
            break
        for item in items:
            item.dropped = True
            total -= item.tokens
    return total


def build_frozen_block(
    *,
    snapshot: StoreSnapshot,
    plan: "AssemblyPlan | None",
    boundary_turn: "Turn",
    turn_log: Sequence["Turn"],
    policy: Policy,
    counter: "TokenCounter",
    focus: Sequence[str] = (),
    last_touch: Mapping[str, int],
) -> FrozenBlock:
    """构建 F。**只在 Compaction 里调用；纯函数：同输入 → 同字节。**

    ``token_count`` 用 ``counter.count(text)`` 精确计（BPE 不一定对行边界可加）；
    而**预算判定**用逐条累加值 —— 后者只需要单调可比，不需要精确到 1 token，
    且它能解释「为什么这一条进得来、下一条进不来」（§7.2 步骤 4 要求截断点
    只由整数累计决定）。
    """
    generation = (plan.generation + 1) if plan is not None else 1
    header = F_HEADER_FMT % (generation, boundary_turn.index)
    header_tokens = counter.count(header)

    b = _Builder(counter=counter)
    _collect_goal(snapshot, b)
    _collect_pinned(snapshot, turn_log, b)
    _collect_constraints(snapshot, b)
    _collect_reversals(snapshot, b)
    _collect_pending(snapshot, b)
    _collect_skeleton(snapshot, b)
    _collect_events(snapshot, b)

    f_soft = policy.f_soft_budget()
    f_hard = policy.f_hard_cap()
    # warn：软预算超了照留，只有硬上限才真截；truncate：软预算就是上限。
    limit = f_hard if policy.mandatory_overflow == "warn" else f_soft

    total = header_tokens + sum(i.tokens for i in b.items if not i.dropped)
    for group in SACRIFICE_ORDER:
        if total <= limit:
            break
        total = _sacrifice(b, group, total, limit)

    aborted = False
    abort_reason: str | None = None
    if total > limit:
        # 剩下的只有 T0 / T1 / T5 的 T 名 / T7 / 块首行 —— 再牺牲就要动钉住项。
        # 「中止压缩」而非「截断钉住项」：截断一条硬约束的代价是后续所有轮次都可能
        # 违规，而不压缩的代价只是省得少一点。取正确性（§7.3）。
        aborted = True
        abort_reason = (
            "冻结块强制层超出%s上限（上限 %d，实测 %d）：继续压缩必须牺牲钉住项，"
            "按 §7.3 中止本次压缩，沿用旧冻结块"
            % ("硬" if policy.mandatory_overflow == "warn" else "软", limit, total))

    # T6：只吃剩余额度，装到超限即停（**不是**按比例截断）
    t6_items: list[_Item] = []
    if not aborted:
        t6_items = _collect_relevant(
            snapshot, b,
            query=boundary_turn.user_text, turn_index=boundary_turn.index,
            policy=policy, focus=focus, last_touch=last_touch,
        )
        remaining = max(0, f_soft - total)
        full = False
        for item in t6_items:
            if full or item.tokens > remaining:
                # 「装到超限即停」：第一条装不下的就是截断点，**不跳过它去捡更小的**。
                # 跳过会让截断结果依赖"哪条恰好更短"，而那不是可解释的规则。
                full = True
                item.dropped = True
            else:
                remaining -= item.tokens

    # 渲染：按 tier 顺序，跳过被牺牲的条目
    lines: list[str] = [header]
    for tier in TIER_ORDER:
        for item in b.of_tier(tier):
            if not item.dropped:
                lines.extend(item.lines)
    text = "".join(lines)

    tier_stats: dict[FTier, TierStat] = {}
    for tier in TIER_ORDER:
        items = b.of_tier(tier)
        kept = [i for i in items if not i.dropped]
        tier_stats[tier] = TierStat(
            included=len(kept),
            omitted=len(items) - len(kept),
            tokens=sum(i.tokens for i in kept),
        )

    if aborted:
        # 中止时正文为空：Session 会据此沿用旧 F，本次冻结成果不被采用。
        return FrozenBlock(
            text="", sha16=sha16(""), token_count=0,
            tier_stats=tier_stats, aborted=True, abort_reason=abort_reason,
        )

    return FrozenBlock(
        text=text,
        sha16=sha16(text),
        token_count=counter.count(text),
        tier_stats=tier_stats,
        aborted=False,
        abort_reason=None,
    )


def empty_frozen_block() -> FrozenBlock:
    """空冻结块（不携带任何内容）。给「有 plan 但 F 为空」的极端场景用。"""
    text = ""
    return FrozenBlock(
        text=text, sha16=sha16(text), token_count=0,
        tier_stats={tier: TierStat(0, 0, 0) for tier in TIER_ORDER},
        aborted=False, abort_reason=None,
    )


def frozen_from_plan(plan: "AssemblyPlan") -> FrozenBlock:
    """★ 从 plan 取回 F 的**外壳**，正文逐字照抄 —— 装配期唯一允许的 F 来源。

    这就是 I6 的落地函数：``text`` / ``sha16`` / ``token_count`` 全部从 plan 读，
    一个字符都不重算。``tier_stats`` 留空，因为它是**边界那一刻的记账**，
    不是装配期需要的东西；要读 tier 统计请查 ``CompactionEvent``。

    为什么不能"读不到就重建"：``build_frozen_block`` 是纯函数，重建总是能成功，
    于是"装配期重算"就悄悄回来了 —— 重算用的是**当前**键树，而边界之间
    ``attribute()`` 已经改过库，算出来必然与冻结那一代不同 → F 字节变 → 前缀断，
    且没有任何报错。所以本函数**没有**任何降级分支：plan 里的正文就是正文。
    """
    return FrozenBlock(
        text=plan.frozen_text,
        sha16=plan.frozen_sha16,
        token_count=plan.frozen_tokens,
        tier_stats={},
    )


__all__ = [
    "FTier",
    "FrozenBlock",
    "SACRIFICE_ORDER",
    "TIER_LABEL",
    "TIER_ORDER",
    "TierStat",
    "build_frozen_block",
    "empty_frozen_block",
    "frozen_from_plan",
]
