"""边界执行器：事务性重冻结。

一次边界做三件事，且必须原子（STORE-SCHEMA 事务 T3）：

1. ``build_frozen_block`` 产出新 F 的**字节**（纯函数，失败即本次不压缩）；
2. 用新 F 造新一代 ``AssemblyPlan``（``generation+1``，
   ``r_first_turn = max(1, at_turn - K + 1)``），**F 的正文进 plan**（I6 / P8）；
3. 单事务写 plan + CompactionEvent。

为什么必须一个事务：分两次写，中间崩溃会得到「边界存在但冻结块为空」——
该区间的内容既不在窗口里、也不在冻结块里，**凭空消失**。这是本设计里唯一
会造成不可恢复丢失的形态（STORE-SCHEMA T3）。``plans.frozen_text`` 是
``NOT NULL``，所以那个状态在结构上就不存在。

**T3 的写集只有 ``plans`` + ``event``**（I16）：不 ``UPDATE store``，连
``updated_at`` 也不碰。压缩不改键树，因此不得让 ``rev`` 变化 —— 一旦掺入
「操作次数」，此后每一次 no-op 判定都拿错基准，而两个方向都静默。

失败语义（F11）：冻结块构建失败 → 抛 ``FreezeError``，由 Session 转 ``no_compact``
（**不改变存储**）。因此「压缩失败」永远等价于「R 段继续增长」，
而不是「历史丢了一半」。

依赖：``freeze`` / ``render`` / ``model`` / ``policy`` / ``text``（不做存储实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from .errors import FreezeError, InvariantError, StoreError
from .freeze import FTier, TIER_ORDER, FrozenBlock, build_frozen_block
from .model import AssemblyPlan, CompactionEvent, Message, Role
from .render import render_message, render_r_segment, render_turn
from .text import FMT_SCHEMA, snapshot_id as _snapshot_id

if TYPE_CHECKING:
    from .policy import BoundaryReason, Policy
    from .model import StoreSnapshot, SystemPrompt, Turn
    from .store import StorePort
    from .tokens import TokenCounter


@dataclass(frozen=True, slots=True)
class CompactionResult:
    plan: AssemblyPlan
    frozen: FrozenBlock
    event: CompactionEvent


def compact(
    *,
    store: "StorePort",
    snapshot: "StoreSnapshot",
    turn_log: Sequence["Turn"],
    at_turn: "Turn",
    reason: "BoundaryReason",
    policy: "Policy",
    counter: "TokenCounter",
    system: "SystemPrompt",
    focus: Sequence[str] = (),
    last_touch: Mapping[str, int] | None = None,
    tokens_before: int | None = None,
) -> CompactionResult:
    """执行一次压缩边界。

    ``turn_log`` 是边界轮**之前**的全部轮次（不含 ``at_turn`` 本身）；
    ``tokens_before`` 可由调用方给出（通常是触发边界的那次装配估算），
    缺省时按「原文规模」计算：``count(S) + Σ count(render_turn)``。

    **顺序是「先压缩、再装配」**（不是「装配→发现超预算→压缩」）：本函数全程
    不装配 payload，只产出 plan 与 event。装配由调用方在新 plan 上做一次 ——
    那一次装配同时就是「边界轮返回的 payload」，因此边界轮的 estimate 只算一次，
    F 也只生成一次（F 生成会更新 ``epoch_turn`` 语义，算两次就错两次）。
    """
    if not snapshot.topics and policy.on_empty_store == "no_compact":
        raise FreezeError(
            "存储里没有任何事物节点：调用方从未 attribute()，压缩必然丢掉全部信息。"
            "按 policy.on_empty_store=\"no_compact\" 放弃这次压缩（R 段继续增长，"
            "行为退化为 A 臂，但信息不丢）")

    frozen = build_frozen_block(
        snapshot=snapshot,
        plan=snapshot.plan,
        boundary_turn=at_turn,
        turn_log=turn_log,
        policy=policy,
        counter=counter,
        focus=focus,
        last_touch=last_touch or {},
    )
    if frozen.aborted:
        raise FreezeError(frozen.abort_reason or "冻结块构建被中止（未给出原因）")
    if not frozen.text:
        # `plans.frozen_text` 是 NOT NULL + length≥1：空 F 落不了库。
        # 与其让 DDL 抛一个 CHECK 失败（会被映射成"存储损坏"），不如在这里说清楚。
        raise FreezeError(
            "冻结块正文为空：空 F 无法落库（plans.frozen_text 要求 length ≥ 1）。"
            "请在存储里先写入至少一条事物/目的，再触发边界")

    old_generation = snapshot.plan.generation if snapshot.plan is not None else 0
    generation = old_generation + 1
    r_first_turn = max(1, at_turn.index - policy.verbatim_keep + 1)

    plan = AssemblyPlan(
        schema=FMT_SCHEMA,
        generation=generation,
        epoch_turn=at_turn.index,
        r_first_turn=r_first_turn,
        frozen_text=frozen.text,          # ★ I6：F 的字节随 plan 落库，装配期只读不算
        frozen_sha16=frozen.sha16,
        frozen_tokens=frozen.token_count,
        system_sha16=system.sha16,
        # P6：只由 (schema, system_sha16, frozen_sha16) 派生 —— 不含键树/rev/边界。
        snapshot_id=_snapshot_id(
            schema=FMT_SCHEMA, system_sha16=system.sha16, frozen_sha16=frozen.sha16),
        # 审计字段：写本行时的 rev。只写不判（A7）。
        store_rev=snapshot.rev,
        counter_id=counter.counter_id,
        policy_digest=policy.digest(),
    )

    if tokens_before is None:
        tokens_before = _verbatim_tokens(
            system=system, turn_log=turn_log, counter=counter)

    included = {tier.value: frozen.tier_stats[tier].included for tier in TIER_ORDER}
    omitted = {tier.value: frozen.tier_stats[tier].omitted for tier in TIER_ORDER}

    event = CompactionEvent(
        generation=generation,
        at_turn=at_turn.index,
        reason=reason,
        included=included,
        omitted=omitted,
        frozen_sha16=frozen.sha16,
        frozen_tokens=frozen.token_count,
        aborted=False,
        abort_reason=None,
    )

    # 边界后的 payload 规模：R 段 = turns[r_first .. at_turn-1]，U 段 = 本轮 user。
    # 这是 STORE-SCHEMA ``plans.tokens_after`` 的取值；`AssemblyPlan` 里没有它的
    # 载体（那是测量字段，不是装配输入），所以走 put_plan 的内部扩展参数。
    tokens_after = tokens_after_boundary(
        system=system,
        frozen=frozen,
        turns_after=tuple(t for t in turn_log if t.index >= r_first_turn),
        user_text=at_turn.user_text,
        counter=counter,
        policy=policy,
    )

    try:
        with store.begin(write=True) as txn:
            txn.put_system(system)
            # ★ 号由 ``put_plan`` 在**写事务内**分配并返回（ARCHITECTURE §10.4）。
            # 上面那个 ``generation`` 只是候选号：两个驱动拿同一份 snapshot 算，会算出
            # 同一个候选 —— 真实号以返回值为准，于是它们永远错开。
            #
            # 返回值**必须**采纳回内存（plan 与 event 都要），否则下一轮压缩会用旧号
            # 再算一次，候选与新号重新错开，症状是"改完反而一压就死"。
            # 对 payload 零影响：``generation`` 不在冻结字节里（P6 的 snapshot_id 也不含它）。
            allocated = txn.put_plan(plan, tokens_before=tokens_before, tokens_after=tokens_after)
            if allocated != plan.generation:
                plan = replace(plan, generation=allocated)
                event = replace(event, generation=allocated)
            txn.append_event(event)
            txn.commit()
    except (StoreError, InvariantError):
        # ★ 已经**带分类意义**的异常原样上抛，一个字都不包。
        #
        # 分类以异常类型为键（§10.4）。包装会把类型抹成 ``StoreError``，于是
        # ``InvariantError``（压缩器算错 → 只停压缩）与 ``StoreError``（写盘失败）
        # 变得无法区分 —— 实测后果：一次 ``plans`` 约束违反被抄成 ``store_io``，
        # 会话按"磁盘写不进去"处置，而真正的原因（压缩算错）在报告里消失。
        # 这是"错误分类最不该出错的地方"：包一层看起来更友好，代价是诊断归零。
        raise
    except Exception as exc:  # noqa: BLE001 - 端口实现可能抛任意异常
        # 只有**没有分类**的异常才需要包：端口实现（第三方）抛什么都不该决定降级语义。
        raise StoreError("落压缩边界失败：%s" % exc) from exc

    return CompactionResult(plan=plan, frozen=frozen, event=event)


def tokens_after_boundary(
    *,
    system: "SystemPrompt",
    frozen: FrozenBlock,
    turns_after: Sequence["Turn"],
    user_text: str,
    counter: "TokenCounter",
    policy: "Policy",
) -> int:
    """边界后的 payload token 规模（测量记账用，**不参与装配**）。

    口径与 ``assemble`` 完全一致：payload.token_count 就是各块 token 数之和。
    各段复用同一批渲染原语（``render_r_segment`` / ``render_message``），
    所以这里的数与随后那次真实装配不会漂 —— 若各拼一份，两个数会各自
    "看起来很确定"，只是彼此矛盾。
    """
    from .render import MISSING_REPLY_MARKER

    marker = MISSING_REPLY_MARKER if policy.on_missing_assistant == "mark" else None
    return (
        counter.count(system.text)
        + counter.count(frozen.text)
        + counter.count(render_r_segment(turns_after, missing_marker=marker))
        + counter.count(render_message(Message(Role.USER, user_text)))
    )


def _verbatim_tokens(
    *, system: "SystemPrompt", turn_log: Sequence["Turn"], counter: "TokenCounter"
) -> int:
    """「边界前的原文规模」：S + 全部轮次的逐字渲染。诚实记账用，不参与装配。"""
    total = counter.count(system.text)
    for turn in turn_log:
        total += counter.count(render_turn(turn))
    return total


def tier_omitted(event: CompactionEvent, tier: FTier) -> int:
    return int(event.omitted.get(tier.value, 0))


def describe_event(event: CompactionEvent) -> str:
    """一行可读摘要（日志/报告用，不进 payload 字节）。"""
    sacrificed = ", ".join(
        "%s-%d" % (k, v) for k, v in sorted(event.omitted.items()) if v
    ) or "无"
    return (
        "边界 gen=%d at_turn=%d reason=%s frozen=%dtok 牺牲=%s"
        % (event.generation, event.at_turn, getattr(event.reason, "value", event.reason),
           event.frozen_tokens, sacrificed)
    )


__all__ = [
    "CompactionResult",
    "compact",
    "describe_event",
    "tier_omitted",
    "tokens_after_boundary",
]
