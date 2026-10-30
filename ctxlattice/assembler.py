"""★ 核心：纯函数装配。

```
payload 字节 = [S] 静态 system → [F] 冻结记忆块 → [R] 原文轮段 → [U] 本轮用户消息
```

**本模块不得 import ``store*`` / ``clock`` / ``os`` / ``time`` / ``random``。**
这不是风格要求，是 A3 的可执行版本：一旦装配读了存储或时钟，F 的字节就会在边界
之间随归因写入而变化，``cached_i`` 归零，而**没有任何报错**。把「取数」与「拼装」
分开，是让确定性成为结构性事实而非纪律要求。

前缀稳定性（I1）在这里是怎么成立的（这是整个方案成立的前提）：

* S 段：会话内恒定（I2）。
* F 段：只在边界重写（I3）。边界轮允许前缀破坏（全价轮，计费侧单独标）。
* R 段：``turns[r_first .. t-1]``。第 t+1 轮相对第 t 轮**只在尾部追加**
  ``render_turn(turn_t)``，而 ``turn_t`` 的第一个片段就是
  ``render_message(USER, turn_t.user_text)`` —— 与第 t 轮的 U 段**同一个函数**产出。
  所以「上一轮的 U 段是新一轮 R 段的开头」是函数级事实，不是约定。
* U 段：本轮的固定投手，永远在最后。

因此 ``assemble`` 里**没有一处**做「按当前轮次重新计算 R 起点」这种动作：
R 的起点只由 ``plan.r_first_turn`` 决定，而它只在边界变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .errors import InvariantError
from .model import (
    BlockKind,
    FrozenView,
    Message,
    Payload,
    Role,
    make_block,
    store_read_count,
)
from .render import MISSING_REPLY_MARKER, render_message, render_r_segment
from .text import sanitize

if TYPE_CHECKING:
    from .model import SystemPrompt, Turn
    from .policy import Policy
    from .tokens import TokenCounter


@dataclass(frozen=True, slots=True)
class AssemblyInputs:
    """装配的全部输入。**没有存储、没有时钟、没有环境变量** —— 这就是 A3。"""

    system: "SystemPrompt"
    frozen: FrozenView | None          # None 仅在「尚无 plan」时出现
    turns: Sequence["Turn"]            # 已按 index 升序，即 turns[r_first_turn:]
    user_text: str
    counter: "TokenCounter"
    policy: "Policy"
    boundary: bool = False
    cold_start: bool = False
    degraded: bool = False
    degrade_reason: str | None = None


def assemble(inputs: AssemblyInputs) -> Payload:
    """纯函数：无 IO、无时钟、无环境读取。

    保证：
      (1) 同 ``(system, frozen, turns, user_text, counter_id, policy.digest())``
          → 逐字节相同；
      (2) 非边界轮：本轮 payload 以上一轮 payload 为字节前缀；
      (3) blocks 顺序恒为 S → F → R → U（缺项跳过，不占位）；
      (4) **执行期间存储读次数为 0** —— 常开运行期断言，见下面的 ``_reads_before``。

    (4) 为什么必须常开而不是 debug-only：``FrozenView`` 只是**结构协议**，
    下面这种实现在类型上完全合法 ——

        @property
        def text(self): return build_frozen_block(...)   # 访问时才重算

    import 图干净、mypy 通过、I3 被破且不报错。前两级（import 图、类型）挡不住
    "属性背后的计算"，只有运行期这一条能挡。它比前缀断言便宜得多（一次整数比较），
    没有理由放到 ``strict_invariants`` 后面。
    """
    _reads_before = store_read_count()
    counter = inputs.counter
    blocks = []

    # ---- S：静态 system。会话内恒定，是 payload 的第 0 个字节。 ----------
    s_text = inputs.system.text
    blocks.append(make_block(BlockKind.S, s_text, counter.count(s_text)))

    # ---- F：冻结记忆块。字节来自 plan.frozen_text，这里只做打包。 ---------
    frozen = inputs.frozen
    if frozen is not None and frozen.text:
        blocks.append(make_block(BlockKind.F, frozen.text, counter.count(frozen.text)))

    # ---- R：原文轮段。块首行恒定存在（见 render.R_HEADER 的注释）。 ------
    marker = MISSING_REPLY_MARKER if inputs.policy.on_missing_assistant == "mark" else None
    r_text = render_r_segment(inputs.turns, missing_marker=marker)
    blocks.append(make_block(BlockKind.R, r_text, counter.count(r_text)))

    # ---- U：本轮用户消息。永远最后。 -----------------------------------
    u_text = render_message(Message(Role.USER, inputs.user_text))
    blocks.append(make_block(BlockKind.U, u_text, counter.count(u_text)))

    # ---- (4) 装配期间零存储读：I6 的运行期闸门 ----------------------------
    if store_read_count() != _reads_before:
        raise InvariantError(
            "装配期间发生了 %d 次存储读取（I3/I6 被破坏）：payload 的字节必须只由 "
            "AssemblyInputs 决定，F 的正文只能来自 plan。最可能的成因是某个 "
            "frozen 对象的 ``.text`` 属性背后在调 build_frozen_block 或查库 —— "
            "类型检查与 import 图都挡不住这种写法。"
            % (store_read_count() - _reads_before))

    return Payload(
        blocks=tuple(blocks),
        counter_id=counter.counter_id,
        degraded=inputs.degraded,
        degrade_reason=inputs.degrade_reason,
        boundary=inputs.boundary,
        cold_start=inputs.cold_start,
    )


# ---------------------------------------------------------------------------
# 前缀稳定性断言（I1 的守护手段）
# ---------------------------------------------------------------------------
def common_prefix_bytes(a: bytes, b: bytes) -> int:
    """公共前缀长度（字节）。短的那一侧先结束就返回短的长度。"""
    limit = min(len(a), len(b))
    # 逐段比较再线性收尾：比逐字节快，且结果完全一致。
    step = 64
    pos = 0
    while pos < limit:
        end = min(pos + step, limit)
        if a[pos:end] != b[pos:end]:
            break
        pos = end
    while pos < limit and a[pos] == b[pos]:
        pos += 1
    return pos


def prefix_violation(prev: Payload, cur: Payload) -> str | None:
    """检查 ``cur`` 是否以 ``prev`` 为前缀。合法返回 None，否则返回中文说明。"""
    prev_bytes = prev.to_bytes()
    cur_bytes = cur.to_bytes()
    if cur_bytes.startswith(prev_bytes):
        return None
    common = common_prefix_bytes(prev_bytes, cur_bytes)
    where = _describe_offset(cur, common)
    return (
        "前缀稳定性（I1）被破坏：本轮 payload 不以上一轮为前缀。\n"
        "  上一轮 %d 字节，本轮 %d 字节，公共前缀 %d 字节（%.1f%%）。\n"
        "  首个分歧位置：%s\n"
        "  上一轮该处：%r\n"
        "  本轮该处  ：%r\n"
        "  上一轮 boundary=%s cold_start=%s sha16=%s\n"
        "  本轮   boundary=%s cold_start=%s sha16=%s\n"
        "  后果：cached_i 退化为 count(S)，每一轮都变成全价轮，且没有任何运行期错误。"
        % (
            len(prev_bytes), len(cur_bytes), common,
            100.0 * common / max(1, len(prev_bytes)),
            where,
            prev_bytes[common:common + 24],
            cur_bytes[common:common + 24],
            prev.boundary, prev.cold_start, prev.sha16,
            cur.boundary, cur.cold_start, cur.sha16,
        )
    )


def _describe_offset(payload: Payload, offset: int) -> str:
    """把字节偏移翻译成「第几个块、块内第几字节」，便于直接定位到代码。"""
    consumed = 0
    for block in payload.blocks:
        size = len(block.text.encode("utf-8"))
        if offset < consumed + size:
            return "块 %s 内偏移 %d（块长 %d 字节，块 sha16=%s）" % (
                block.kind.value, offset - consumed, size, block.sha16)
        consumed += size
    return "越过末尾（offset=%d，总长=%d）" % (offset, consumed)


def assert_prefix_stable(prev: Payload, cur: Payload) -> None:
    """严格断言 ``cur.to_bytes()`` 以 ``prev.to_bytes()`` 为前缀。

    仅 ``strict_invariants=True`` 或测试中调用；破坏即抛 ``InvariantError``。
    生产路径上不抛 —— 抛了也救不回 ``cached_i``，但测试必须炸得足够响。
    """
    detail = prefix_violation(prev, cur)
    if detail is not None:
        raise InvariantError(detail)


def describe_payload(payload: Payload) -> str:
    """单行摘要（日志/报告用，**不进 payload 字节**）。"""
    parts = ["%s:%dB/%dtok" % (b.kind.value, len(b.text.encode("utf-8")), b.token_count)
             for b in payload.blocks]
    flags = []
    if payload.boundary:
        flags.append("boundary")
    if payload.cold_start:
        flags.append("cold_start")
    if payload.degraded:
        flags.append("degraded:%s" % sanitize(payload.degrade_reason or "?", 40))
    suffix = (" [" + ",".join(flags) + "]") if flags else ""
    return "payload sha16=%s tokens=%d %s%s" % (
        payload.sha16, payload.token_count, " ".join(parts), suffix)


__all__ = [
    "AssemblyInputs",
    "assemble",
    "assert_prefix_stable",
    "common_prefix_bytes",
    "describe_payload",
    "prefix_violation",
]
