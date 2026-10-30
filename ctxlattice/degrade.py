"""fail-open 机制与降级事件。

统一原则（§10）：**``send()`` 与 ``observe()`` 永不抛**；``open()`` / ``attribute()`` /
``inspect()`` 按签名抛。捕获范围是 ``Exception``，**不捕获 ``BaseException``** ——
``KeyboardInterrupt`` / ``SystemExit`` 必须穿透。

三条降级路径，按"还剩多少信息"排序：

| 路径 | 装配内容 | 什么时候用 |
|---|---|---|
| ``full_history`` | S + 全量轮次 + U（A 臂等价物） | 装配内部异常（F12） |
| ``buffer_only`` | S + 进程内缓冲的轮次 + U（无 F） | 存储不可读（F1） |
| ``last_resort`` | S + 一行说明 + U | 连全量历史都拼不出（F13） |

三者都走同一个 ``assemble()``，所以降级 payload 的字节形态与正常 payload 完全同构
（同样的块顺序、同样的 ``role: `` 前缀）—— 差别只在"历史段有什么"，
这样一旦降级，bench 侧也能用同一套前缀比对逻辑看出它退化成了什么。

**降级会破坏前缀稳定性**（F 丢掉了），这是不可避免的：``full_history`` 的定义就是
"没有 F"。因此 `Payload.degraded=True` 必须被计费侧当作「本轮不可比」，
而不是悄悄混进均值 —— 这正是 `degraded` 字段存在的理由。

依赖：``assembler`` / ``render`` / ``policy`` / ``errors`` / ``model``。
"""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING, Sequence

from .assembler import AssemblyInputs, assemble
from .errors import (
    AttributionError,
    ClockAnomalyError,
    EncodingError_,
    EventDriftError,
    EventMissingError,
    FreezeError,
    InvariantError,
    LatticeError,
    PolicyError,
    SessionConflictError,
    StoreCorruptError,
    StoreError,
    StoreIOError,
)
from .model import BlockKind, Message, Payload, PayloadBlock, Role, make_block
from .policy import DegradeReason
from .render import R_HEADER, render_message
from .text import sanitize as _sanitize

if TYPE_CHECKING:
    from .model import SystemPrompt, Turn
    from .policy import Policy
    from .tokens import TokenCounter

#: 磁盘/权限类 errno：这些说明"写不下"，不是"写坏了"，降级动作相同但原因不同。
_DISK_ERRNOS = frozenset(
    code for code in (
        errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    )
)

LAST_RESORT_NOTICE = "[降级] 本轮为最小可用输入：历史内容无法装配（详见降级日志）\n"


# ---------------------------------------------------------------------------
# 三条降级装配
# ---------------------------------------------------------------------------
def full_history_payload(
    *,
    system: "SystemPrompt",
    turns: Sequence["Turn"],
    user_text: str,
    counter: "TokenCounter",
    policy: "Policy",
    reason: DegradeReason,
    detail: str,
) -> Payload:
    """A 臂等价物的直发：S + 全量 turns + U。用于任何装配失败时的兜底。"""
    return assemble(AssemblyInputs(
        system=system,
        frozen=None,
        turns=turns,
        user_text=user_text,
        counter=counter,
        policy=policy,
        boundary=False,
        cold_start=False,
        degraded=True,
        degrade_reason="%s: %s" % (reason.value, sanitize(detail)),
    ))


def buffer_only_payload(
    *,
    system: "SystemPrompt",
    turns: Sequence["Turn"],
    user_text: str,
    counter: "TokenCounter",
    policy: "Policy",
    reason: DegradeReason = DegradeReason.STORE_CORRUPT,
    detail: str = "",
) -> Payload:
    """存储不可读时，用进程内轮次缓冲装配（无 F）。

    与 ``full_history_payload`` 的**字节形态完全一致**：两者的差别在语义
    （"存储坏了"vs"装配坏了"），不在字节 —— 因为对 provider 来说，
    "没有 F 的全量历史"就是同一种东西。
    """
    return full_history_payload(
        system=system, turns=turns, user_text=user_text, counter=counter,
        policy=policy, reason=reason, detail=detail,
    )


def last_resort_payload(
    *,
    system: "SystemPrompt",
    user_text: str,
    counter: "TokenCounter",
) -> Payload:
    """最小可用：S + 一行显式说明 + U。内存极紧时的最后一道，**绝不抛**。

    这里连 ``counter`` 都可能坏掉（内存极紧时什么都可能），所以计数失败就用 0 ——
    一个 token 数不准的 payload 也好过抛异常让调用方拿不到任何输入。
    """
    try:
        s_text = system.text
    except Exception:  # noqa: BLE001
        s_text = ""

    def _count(text: str) -> int:
        try:
            return counter.count(text)
        except Exception:  # noqa: BLE001
            return 0

    blocks: list[PayloadBlock] = []
    if s_text:
        blocks.append(make_block(BlockKind.S, s_text, _count(s_text)))
    r_text = R_HEADER + LAST_RESORT_NOTICE
    blocks.append(make_block(BlockKind.R, r_text, _count(r_text)))
    try:
        u_text = render_message(Message(Role.USER, user_text))
    except Exception:  # noqa: BLE001
        u_text = render_message(Message(Role.USER, user_text.encode("utf-8", "backslashreplace")
                                       .decode("utf-8")))
    blocks.append(make_block(BlockKind.U, u_text, _count(u_text)))

    try:
        counter_id = counter.counter_id
    except Exception:  # noqa: BLE001
        counter_id = "unknown"

    return Payload(
        blocks=tuple(blocks),
        counter_id=counter_id,
        degraded=True,
        degrade_reason="%s: 最后兜底（S + U）" % DegradeReason.INTERNAL.value,
        boundary=False,
        cold_start=False,
    )


# ---------------------------------------------------------------------------
# 分类与净化
# ---------------------------------------------------------------------------
def classify(exc: BaseException) -> tuple[DegradeReason, str]:
    """把异常映射成 ``(DegradeReason, 已净化的说明)``。

    映射顺序有讲究：先判**最具体**的子类。``StoreIOError`` 要在 ``StoreError``
    之前判，否则磁盘满会被归成笼统的 "store_io"，报告里就看不出「这台机器磁盘满了」。
    """
    detail = "%s: %s" % (type(exc).__name__, exc)

    # ★ 必须排在 ``StoreError`` 之前（也更在 ``StoreCorruptError`` 之前）：
    # ``SessionConflictError`` 继承 ``StoreError``，任何先命中的 ``StoreError``
    # 分支都会把它吞成 "store_io"/"store_corrupt" —— **新类型白加，而且测试还绿**
    # （``degraded`` 仍是 True，只是原因错了）。这条顺序就是 F16 的全部实现。
    #
    # 反过来，这里**只认类型、不读消息**：类型由调用点（``commit_plan``）决定，
    # 降级层无权重新解释业务，否则故障语义会变成实现的函数（§10.4）。
    if isinstance(exc, SessionConflictError):
        return DegradeReason.CONCURRENT_DRIVER, sanitize(detail)
    if isinstance(exc, StoreIOError):
        code = exc.errno
        if code in _DISK_ERRNOS:
            return DegradeReason.DISK_FULL, sanitize(detail)
        return DegradeReason.STORE_IO, sanitize(detail)
    if isinstance(exc, StoreCorruptError):
        return DegradeReason.STORE_CORRUPT, sanitize(detail)
    if isinstance(exc, StoreError):
        # 未细分的存储失败：交给 errno 再判一次（端口实现可能把 ENOSPC 包在文本里）
        if any(word in detail for word in ("ENOSPC", "No space left", "磁盘满", "空间不足")):
            return DegradeReason.DISK_FULL, sanitize(detail)
        return DegradeReason.STORE_IO, sanitize(detail)
    if isinstance(exc, (UnicodeEncodeError, UnicodeDecodeError, EncodingError_)):
        return DegradeReason.ENCODING_ERROR, sanitize(detail)
    if isinstance(exc, ClockAnomalyError):
        return DegradeReason.CLOCK_ANOMALY, sanitize(detail)
    if isinstance(exc, FreezeError):
        return DegradeReason.FREEZE_ABORT, sanitize(detail)
    if isinstance(exc, EventDriftError):
        return DegradeReason.STORE_CORRUPT, sanitize(detail)
    if isinstance(exc, EventMissingError):
        return DegradeReason.STORE_CORRUPT, sanitize(detail)
    if isinstance(exc, AttributionError):
        return DegradeReason.INTERNAL, sanitize(detail)
    if isinstance(exc, PolicyError):
        # 只在构造期出现；走到这里说明调用方吞了构造期异常，仍要能记一笔
        return DegradeReason.INTERNAL, sanitize(detail)
    if isinstance(exc, InvariantError):
        return DegradeReason.INTERNAL, sanitize(detail)
    if isinstance(exc, LatticeError):
        return DegradeReason.INTERNAL, sanitize(detail)
    return DegradeReason.INTERNAL, sanitize(detail)


def sanitize(detail: str, limit: int = 200) -> str:
    """控制字符转义 + 截断（继承 ``ctx.py._show``）。进 ``DegradationRecord.detail``。"""
    return _sanitize(detail, limit)


__all__ = [
    "LAST_RESORT_NOTICE",
    "buffer_only_payload",
    "classify",
    "full_history_payload",
    "last_resort_payload",
    "sanitize",
]
