"""两臂：``BaselineArm`` / ``LatticeArm``。

**A/B 唯一变量 = 上下文装配方式。** 其余全部由 ``ArmProtocol`` 的签名强制对齐：
同 ``Policy``、同 ``SystemPrompt``（同一 sha16）、同 ``TokenCounter``。

为什么两臂都走 ``assembler.assemble``：如果 A 臂自己拼字符串、B 臂走装配器，
那么"S 段逐字节相同""U 段逐字节相同"就变成了两段代码的巧合，
而不是同一个函数的输出。一旦有人改了其中一处的前缀格式，两臂的差异就
悄悄变成了"格式差异 + 装配差异" —— 而 bench 会把它全部记到 B 臂头上。
共用装配器让这条对齐成为**结构性事实**。

给 bench 的接缝（下一轮直接用）：

* ``ArmProtocol`` —— ``bench/harness.py`` 只依赖这五个成员；
* ``StepResult.payload.to_bytes()`` —— 逐字节货真价实，``bench/metrics.py``
  的 ``cache_prefix_tokens`` 直接拿它做逐 token 比对；
* ``LatticeArm.session`` —— 归因注入点（``session.attribute(lines)``）；
* ``SqliteStore`` / ``MemoryStore`` 的 ``snapshot`` / ``read_turns``
  —— 事后复核"这一轮的 F 是什么、R 段里有哪几轮"。

依赖：``assembler`` / ``session`` / ``model`` / ``policy`` / ``tokens``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

from .assembler import AssemblyInputs, assemble
from .model import BlockKind, Message, Payload, PayloadBlock, Role, SystemPrompt, Turn, make_block
from .session import Session

if TYPE_CHECKING:
    from .clock import ClockPort
    from .policy import Policy
    from .store import StorePort
    from .tokens import TokenCounter


@dataclass(frozen=True, slots=True)
class StepResult:
    payload: Payload
    boundary: bool
    degraded: bool
    degrade_reason: str | None


@runtime_checkable
class ArmProtocol(Protocol):
    """A/B 唯一变量 = 上下文装配方式。其余全部由本协议的签名强制对齐。"""

    name: str

    def reset(self) -> None: ...

    def system_block(self) -> PayloadBlock:
        """两臂必须逐字节相同（``test_arms_parity`` 的第一条断言）。"""
        ...

    def step(self, turn: Turn) -> StepResult: ...

    def observe(self, text: str, *, role: Role = Role.ASSISTANT) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# A 臂
# ---------------------------------------------------------------------------
class BaselineArm:
    """全量历史重发：``S + turns[1..i-1] + U_i``。

    它是"调用方标准做法"的**忠实**模拟：每轮把全部历史整包重发，不做任何裁剪。
    不给它加任何优化（比如去重、截断）—— 那会让 A 臂变成一个稻草人，
    bench 的对比就失去意义。
    """

    name = "baseline"

    def __init__(
        self,
        *,
        system: SystemPrompt,
        counter: "TokenCounter",
        policy: "Policy",
    ) -> None:
        self._system = system
        self._counter = counter
        self._policy = policy
        self._turns: list[Turn] = []

    def reset(self) -> None:
        self._turns = []

    def system_block(self) -> PayloadBlock:
        text = self._system.text
        return make_block(BlockKind.S, text, self._counter.count(text))

    def step(self, turn: Turn) -> StepResult:
        payload = assemble(AssemblyInputs(
            system=self._system,
            frozen=None,
            turns=tuple(self._turns),
            user_text=turn.user_text,
            counter=self._counter,
            policy=self._policy,
            boundary=False,
            cold_start=(turn.index == 1),
        ))
        self._turns.append(Turn(index=turn.index, user_text=turn.user_text,
                                pinned=turn.pinned))
        return StepResult(payload=payload, boundary=False,
                          degraded=False, degrade_reason=None)

    def observe(self, text: str, *, role: Role = Role.ASSISTANT) -> None:
        if not self._turns:
            return
        current = self._turns[-1]
        self._turns[-1] = current.with_reply(Message(role=role, text=text))

    def close(self) -> None:
        return None

    # -- 诊断 -------------------------------------------------------------
    @property
    def turns(self) -> tuple[Turn, ...]:
        return tuple(self._turns)


# ---------------------------------------------------------------------------
# B 臂
# ---------------------------------------------------------------------------
class LatticeArm:
    """本 SDK：内部持有一个 ``Session``，``step()`` = ``session.send()``。"""

    name = "lattice"

    def __init__(
        self,
        *,
        store: "StorePort",
        system: SystemPrompt,
        counter: "TokenCounter",
        policy: "Policy",
        clock: "ClockPort | None" = None,
    ) -> None:
        self._store = store
        self._system = system
        self._counter = counter
        self._policy = policy
        self._clock = clock
        self._session = Session.open(
            store=store, policy=policy, clock=clock,
            counter=counter, system=system,
        )

    #: bench 的归因注入点：``arm.session.attribute(lines)``
    @property
    def session(self) -> Session:
        return self._session

    def reset(self) -> None:
        """重开会话（**不删库**）：已冻结的 F 与轮次缓冲从存储恢复。

        不提供"清空库"的语义：删除是不可逆操作，必须显式确认（N8）。
        要一份干净的对照，就给一个新库。
        """
        try:
            self._session.close()
        finally:
            self._session = Session.open(
                store=self._store, policy=self._policy, clock=self._clock,
                counter=self._counter, system=self._system,
            )

    def system_block(self) -> PayloadBlock:
        text = self._system.text
        return make_block(BlockKind.S, text, self._counter.count(text))

    def step(self, turn: Turn) -> StepResult:
        payload = self._session.send(turn.user_text, pinned=turn.pinned)
        return StepResult(
            payload=payload,
            boundary=payload.boundary,
            degraded=payload.degraded,
            degrade_reason=payload.degrade_reason,
        )

    def observe(self, text: str, *, role: Role = Role.ASSISTANT) -> None:
        self._session.observe(text, role=role)

    def close(self) -> None:
        self._session.close()

    # -- 诊断 -------------------------------------------------------------
    def describe(self) -> str:
        return "arm=lattice " + self._session.describe()


# ---------------------------------------------------------------------------
# 两臂对齐的强制检查（bench 与测试共用）
# ---------------------------------------------------------------------------
def assert_arms_aligned(a: ArmProtocol, b: ArmProtocol) -> None:
    """两臂 S 段逐字节相同。**这是 bench 有效性的前提，不是可选项。**"""
    left = a.system_block()
    right = b.system_block()
    if left.sha16 != right.sha16 or left.text != right.text:
        from .errors import InvariantError

        raise InvariantError(
            "两臂的 S 段不逐字节相同：%s sha16=%s（%d 字节），%s sha16=%s（%d 字节）。"
            "S 段不同意味着 payload 从第 0 个字节就分叉，前缀命中率"
            "与两臂对比同时失效"
            % (a.name, left.sha16, len(left.text.encode("utf-8")),
               b.name, right.sha16, len(right.text.encode("utf-8"))))


def arm_summary(results: Sequence[StepResult]) -> str:
    """一段可读摘要（报告用，不进 payload 字节）。"""
    total = len(results)
    boundaries = sum(1 for r in results if r.boundary)
    degraded = sum(1 for r in results if r.degraded)
    tokens = sum(r.payload.token_count for r in results)
    return "轮次 %d 边界 %d 降级 %d 累计 payload token %d" % (
        total, boundaries, degraded, tokens)


__all__ = [
    "ArmProtocol",
    "BaselineArm",
    "LatticeArm",
    "StepResult",
    "arm_summary",
    "assert_arms_aligned",
]
