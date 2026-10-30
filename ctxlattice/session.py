"""会话 —— 唯一有状态的编排者。

状态机（§5.4）：

```
CLOSED ──open()──► READY ──send(U)──► ASSEMBLING ──► AWAIT_REPLY
                     ▲                    │               │
                     │                    ├─边界─► COMPACTING
                     └──── observe(...) ──┴───────────────┘
```

**fail-open 的边界画在这里**（A4 / F2）：

* ``open()`` / ``attribute()`` / ``inspect()`` → **按签名抛**。
  参数/依赖/结构错误必须大声失败：调用方还能做选择（换库、升级 schema、
  显式退回 BaselineArm）。静默降级会让"配置错误"伪装成"运行正常"。
* ``send()`` / ``observe()`` / ``close()`` → **永不抛**（只吞 ``Exception``，
  ``KeyboardInterrupt`` / ``SystemExit`` 必须穿透）。

``send()`` 的关键时序（§5.1）：**payload 字节在写盘之前就已确定**。
写盘是"尽力而为的旁路"，所以磁盘满只影响"下次还能不能压缩"，
不影响"这一轮发什么"（§10.2）。把副作用移出关键路径，比事后补偿更可靠。

确定性前提（写进本 docstring 并作为测试断言）：

    同一 store 快照 ∧ 同一轮 user_text ∧ 同一 system ∧ 同一 policy.digest()
    ∧ 同一 counter_id ∧ 同一 plan.generation
    ⇒ payload.to_bytes() 逐字节相同
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from .assembler import AssemblyInputs, assemble, assert_prefix_stable
from .attribution import parse_delta
from .clock import ClockPort, SystemClock, safe_tick
from .compaction import CompactionResult, compact
from .degrade import (
    buffer_only_payload,
    classify,
    full_history_payload,
    last_resort_payload,
    sanitize,
)
from .errors import (
    AttributionError,
    FreezeError,
    LatticeError,
    PolicyError,
    SessionConflictError,
    StoreError,
    StoreIOError,
)
from .freeze import FrozenBlock, frozen_from_plan
from .model import (
    CompactionEvent,
    MergeStat,
    Message,
    Payload,
    Role,
    SessionStats,
    StoreSnapshot,
    SystemPrompt,
    Turn,
)
from .policy import BoundaryReason, DegradeReason, Policy, pick_reason
from .render import normalize_prompt
from .store import StorePort
from .text import FMT_SCHEMA, encode_or_raise, ensure_utf8, normalize, sha16_text
from .text import snapshot_id as _snapshot_id

if TYPE_CHECKING:
    from .model import AssemblyPlan, StoreSnapshot
    from .tokens import TokenCounter

#: 降级事件环形缓冲上限（§10.3）。超出丢最旧并计数 —— 不无限增长，
#: 否则"内存极紧"这种最需要它的场景会先被它自己撑爆。
DEGRADATION_RING = 1024

BUILTIN_SYSTEM_SOURCE = "builtin:prompts/system.md"

#: P9 检出每轮比对**最近几轮**已提交的轮次（不含本轮：本轮还没进缓冲）。
#:
#: 为什么是常数 3 而不是 1：一轮里最多可能有一轮是我写的、一轮是别人写的，
#: 再加上"上一次怀疑"要复读一次，3 保证同一个不一致在两次观察里都落在窗口内。
#: 窗口只是"看多宽"，不影响判定 —— 判定仍然是"两次数值相同的不一致"。
FOREIGN_PROBE_WINDOW = 3


class Phase(str, Enum):
    READY = "ready"
    ASSEMBLING = "assembling"
    AWAIT_REPLY = "await_reply"
    COMPACTING = "compacting"
    DEGRADED = "degraded"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# system 提示的构造
# ---------------------------------------------------------------------------
def _builtin_system_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "system.md")


def make_system_prompt(system: "SystemPrompt | str | None") -> SystemPrompt:
    """把 ``None`` / ``str`` / ``SystemPrompt`` 统一成规范化的 ``SystemPrompt``。

    S 段是 payload 的第 0 个字节，且**整个会话不再变**（I2）。所以：
    * 规范化必须幂等（行尾、结尾换行、行尾空白）；
    * 不可编码的 S **直接报错**（``EncodingError_``）而不是静默转义 ——
      静默转义会让两臂的 S 段看起来"逐字节相同"而实际内容已被悄悄改写。
    """
    if system is None:
        path = _builtin_system_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise LatticeError(
                "读不到内置 system 提示：%s（%s）。请显式传入 system 参数"
                % (path, exc.strerror or exc)) from exc
        encode_or_raise(raw, "内置 system 提示 %s" % path)
        text = normalize_prompt(raw)
        return SystemPrompt(text=text, sha16=sha16_text(text), source=BUILTIN_SYSTEM_SOURCE)

    if isinstance(system, str):
        encode_or_raise(system, "调用方传入的 system 提示")
        text = normalize_prompt(system)
        return SystemPrompt(text=text, sha16=sha16_text(text), source="caller")

    encode_or_raise(system.text, "调用方传入的 SystemPrompt.text")
    expected = sha16_text(system.text)
    if system.sha16 != expected:
        raise PolicyError(
            "SystemPrompt.sha16 与 text 不符：声明 %s，实测 %s。"
            "sha16 是 S 段「逐字节相同」的唯一凭据，对不上就无法证明两臂的 S 段一致"
            % (system.sha16, expected))
    return system


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class Session:
    """一轮请求的编排者。构造走 ``open()``，不要直接实例化。"""

    def __init__(
        self,
        *,
        store: StorePort,
        policy: Policy,
        clock: ClockPort,
        counter: "TokenCounter",
        system: SystemPrompt,
    ) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock
        self._counter = counter
        self._system = system

        self._phase = Phase.READY
        self._turn_log: list[Turn] = []
        self._next_index = 1
        self._plan: AssemblyPlan | None = None
        self._frozen: FrozenBlock | None = None
        self._prev_payload: Payload | None = None

        self._focus: list[str] = []
        self._last_touch: dict[str, int] = {}

        self._degradations: list = []
        self._degradation_dropped = 0
        self._last_snapshot_id: str | None = None

        self._readable = True
        self._writable = True
        #: ★ §10.5 的落点：会话**当前**是否已经不在设计路径上，以及原因。
        #:
        #: 为什么是一个字段而不是每轮从 ``_readable and _writable`` 重新推断：
        #: 推断要逐条复刻 14 条降级分支的判定逻辑，**漏一个分支不会报错**，
        #: 只会让某一类退化永久静默（F3 磁盘满、F1 打开即坏都是这么漏的）。
        #: 记账式的写法只有一个入口（``_note_degraded_mode``）和一个出口
        #: （``_clear_degraded_mode_if_healthy``），新增故障类型时自动带上标记。
        #:
        #: 语义边界：它回答的是"这一轮的 payload 是不是设计路径产出的"，
        #: 与"严重不严重"无关（那是 ``reason``）。所以清空条件必须**读侧写侧都健康**——
        #: 只丢 ``_writable`` 时读侧仍可能坏着，那时把它清掉就是反向漏标。
        self._degraded_mode: DegradeReason | None = None
        #: F16：探测到"同一 session 被两个驱动碰了"（P9 被违反）之后**彻底停写**。
        #: 和 ``_writable`` 分开是必要的：``_writable=False`` 还包含磁盘满（F3，
        #: 等等会好，下次 attribute() 成功就能恢复），而这里是"你的进程模型错了，
        #: 本进程再写下去只会把两个驱动的内容交错进同一条轮次链"。判据是
        #: **不再发起写事务**，不是"不再报错"——只吞异常不停写，等于每轮都撞一次。
        self._writes_halted = False
        #: 压缩器坏了（``InvariantError``）：此后**不再尝试压缩**，但库是好的。
        #:
        #: 为什么必须是第三个状态位，而不是复用上面两个：
        #: * ``_readable=False`` 的意思是"库读不出来"，拿它盖住一个**库完全健康**的
        #:   会话，会让所有读路径一起停掉（连 ``attribute()`` 的归因都做不了）；
        #: * ``_writes_halted`` 的意思是"P9 被违反、不可逆、怪调用方"，把"我们自己
        #:   压缩算错了"塞进去，等于让最重的那个动作背一个可修的原因。
        #:
        #: 判据：**一个状态位只能有一个含义。** 三个位各自对应一句可执行的处置：
        #: 换成缓冲装配 / 停写 / 只停压缩。
        self._compaction_blocked: DegradeReason | None = None
        #: P9 检出② 的"第一次观察"。同一处不一致要连续两次才成立（§10.6：
        #: 不可逆动作要求更强的证据），所以必须记住上一次怀疑的是哪一轮、长什么样。
        self._suspect_turn: int | None = None
        self._suspect_signature: tuple | None = None
        #: 已见轮次指纹：``(turn_no, part) -> content_hash``，只记**第一次**。
        #: 用 setdefault 而不是 update：如果每轮都刷新，"改写"就永远显形不了。
        self._turn_hashes_seen: dict[tuple[int, int], str] = {}
        self._boundaries = 0
        self._last_tokens = 0
        self._peak_tokens = 0
        self._last_event: CompactionEvent | None = None
        #: 库为空（F9）时放弃压缩，直到下一次 attribute() 成功为止。
        #: 不加这个开关的话，40 轮无归因的会话会记 40 条完全相同的降级事件。
        self._suppress_compact = False
        #: 是否**曾经**建成过 plan。用来区分「从未有过」（COLD_START）与
        #: 「有过但不可用」（PLAN_INVALID）—— 两者的语义不同，混用会让
        #: 报告里的边界原因失真。
        self._ever_planned = False

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def open(
        cls,
        *,
        store: StorePort,
        policy: Policy | None = None,
        clock: ClockPort | None = None,
        counter: "TokenCounter" | None = None,
        system: "SystemPrompt | str | None" = None,
    ) -> "Session":
        """可能抛：``PolicyError`` / ``StoreError`` / ``EncodingError_`` / ``LatticeError``。

        A4：构造期**不 fail-open**。配置错误必须大声失败（F2）。
        """
        from .tokens import make_counter as _make_counter

        chosen_policy = policy if policy is not None else Policy()
        chosen_policy.validate()
        chosen_counter = counter if counter is not None else _make_counter(chosen_policy)
        chosen_clock = clock if clock is not None else SystemClock()
        prompt = make_system_prompt(system)

        store.open()  # 存储层自己的受控失败在这里抛（F2）

        session = cls(
            store=store,
            policy=chosen_policy,
            clock=chosen_clock,
            counter=chosen_counter,
            system=prompt,
        )
        session._seed()  # 可能抛 StoreError：第一次打开即损坏 → 大声失败
        return session

    def _seed(self) -> None:
        """从存储恢复 plan / F / 轮次缓冲。

        **读不出来不抛**（F1，不是 F2）：轮次有洞、头部计数不符这类损坏的检测点
        就在 ``read_turns`` / ``snapshot``，而它的补救是"跳过存储、用进程内缓冲装配"。
        若在构造期直接抛，用户连一轮都发不出去 —— 而"发得出去但退化"本来就是
        这套设计承诺的行为。F2 留给真正打不开的库（文件损坏/权限/版本过高），
        那由 ``store.open()`` 抛。
        """
        try:
            snap = self._store.snapshot()
        except StoreError as exc:
            self._mark_unreadable(DegradeReason.STORE_CORRUPT)
            self._record(DegradeReason.STORE_CORRUPT,
                         "打开会话时快照读不出来（退化为仅用进程内缓冲）：%s" % exc,
                         fallback="buffer_only")
            return

        self._last_snapshot_id = snap.snapshot_id
        self._readable = True
        self._clear_degraded_mode_if_healthy()   # 读侧写侧都健康才算走完设计路径

        try:
            if snap.turn_hi >= snap.turn_lo >= 1:
                self._turn_log = list(self._store.read_turns(snap.turn_lo, snap.turn_hi))
                self._next_index = snap.turn_hi + 1
        except StoreError as exc:
            # 历史读不回来：**不要**假装它有。缓冲留空、标记只读，
            # 本轮起就从 R 段为空开始 —— 丢历史但绝不编造历史。
            self._mark_unreadable(DegradeReason.STORE_CORRUPT)
            self._turn_log = []
            self._next_index = 1
            self._record(DegradeReason.STORE_CORRUPT,
                         "历史轮次读不出来（轮次有洞或损坏）：%s" % exc,
                         fallback="buffer_only")

        self._plan = snap.plan
        if snap.plan is not None:
            self._ever_planned = True
            self._frozen = self._restore_frozen(snap.plan)

    def _restore_frozen(self, plan: "AssemblyPlan") -> FrozenBlock | None:
        """从 plan 取回 F 的**外壳**（I6）。**不重算，也没有"读不到就重建"的分支。**

        ``plan.frozen_text`` 就是 F 的全部真相：它是落库的字节，不是派生量。
        唯一的校验是**自洽**（``frozen_sha16`` 对不对得上正文），不符即判
        ``PLAN_INVALID`` 走一次重建边界（等价全价轮）。

        为什么没有"读不到就 build_frozen_block"的退路：那等于让装配在重启后
        用**当前**键树重算 F —— 边界之间 ``attribute()`` 已经改过库，重算结果
        必然与冻结那一代不同，前缀断，而且不报错（§5.4 那个 4–16 KB 的代价，
        换的就是这条路被物理堵死）。
        """
        actual = sha16_text(plan.frozen_text)
        if actual != plan.frozen_sha16:
            self._record(DegradeReason.PLAN_REBUILT,
                         "plan gen=%d 落库的冻结块被改坏：frozen_sha16=%s，正文实测 %s"
                         % (plan.generation, plan.frozen_sha16, actual), fallback="buffer_only")
            self._plan = None
            return None
        return frozen_from_plan(plan)

    # -- 一轮的正常节奏 ---------------------------------------------------
    def send(self, user_text: str, *, pinned: bool = False) -> Payload:
        """★ 唯一入口。**任何内部异常都不会传出去**（A4）。

        ``pinned`` 是 ARCHITECTURE §4.1 ``Turn.pinned`` 的入口（transcript 的
        ``pinned:true`` 靠它进来）；不传则等价于 ``False``。
        """
        try:
            return self._send(user_text, pinned=pinned)
        except Exception as exc:  # noqa: BLE001 - A4：send 永不抛
            return self._emergency(user_text, exc)

    def _send(self, user_text: str, *, pinned: bool) -> Payload:
        if self._phase is Phase.CLOSED:
            raise LatticeError("会话已关闭（close() 之后不能再 send）")

        index = self._next_index
        escaped, was_escaped = ensure_utf8(user_text, "第 %d 轮用户消息" % index)
        # degraded 的初值来自**会话模式**，不是本轮事件（§10.5 的防漏标方向）：
        # 「初值 True，只有确认走完设计路径才置 False」。库一旦不在设计路径上
        # （读不出 / 写不进 / 压缩失败），此后每一轮 payload 都不是设计路径产出的
        # —— 即便这一轮什么异常都没发生。反过来写（初值 False、出错才置 True）
        # 等于要求 14 条分支逐条记得标记，漏一条就永久静默。
        # 本轮新发生的降级只覆盖**原因**（下面各处都是 ``or``），不改变"已经退化"
        # 这个既有事实。
        #
        # 注意这里**不** ``_record()``：事件只在"检测到"的那一刻记一次。
        # 每轮补记会把 1024 条环形缓冲刷爆，真正的原因被挤出去 —— 比不记更坏。
        degraded = self._degraded_mode is not None
        degrade_reason: str | None = (
            self._degraded_mode.value if self._degraded_mode is not None else None)
        if was_escaped:
            degraded = True
            degrade_reason = pick_reason(degrade_reason, DegradeReason.ENCODING_ERROR.value)
            self._record(DegradeReason.ENCODING_ERROR,
                         "第 %d 轮用户消息含孤立代理项，已用 backslashreplace 转义" % index,
                         fallback="buffer_only")

        turn = Turn(index=index, user_text=escaped, pinned=pinned)

        # —— 10. F10：连续两次 send（上一轮回复没登记） ——
        if self._phase is Phase.AWAIT_REPLY:
            degraded = True
            degrade_reason = pick_reason(degrade_reason, DegradeReason.MISSING_ASSISTANT.value)
            self._record(DegradeReason.MISSING_ASSISTANT,
                         "第 %d 轮回复未登记就进入第 %d 轮：R 段插入固定标记，不补造"
                         % (index - 1, index), fallback="buffer_only")

        # —— 5'. 存储健康探针（F1 的检测点，**结果不进装配**） ——
        #
        # 为什么必须有：F1 的检测点是 ``snapshot()`` / ``read_turns()``，而
        # ``_seed`` 只在 open 时调用一次。少了这个探针，一个"打开之后才坏"的库
        # 到会话结束都不会被发现 —— payload 照常产出、报告全绿、坏数据没人知道。
        #
        # 为什么它不破坏 A3：探针的**返回值被丢弃**，只有"健康/不健康"这一个布尔
        # 进控制流。装配输入仍然来自内存缓冲 + plan，一个字节都不从库里取。
        # 探针失败 == F1：丢弃 plan/F，退化为"仅缓冲"（A 臂行为），并如实标记。
        if self._readable:
            try:
                health = self._store.snapshot()
                # 顺手读回最后一轮：``read_turns`` 的两处常开断言（条数 + 逐值出现）
                # 就在这条路径上。只读它的**存在性**，绝不拿库里的内容去替换缓冲 ——
                # 写盘是旁路，库可能比缓冲落后一轮（F3），那不是损坏。
                if health.turn_hi >= 1:
                    # 先走一遍纯 ``read_turns``：F1 的检测点就在这里（P3 的两处常开
                    # 断言也在它里面）。它只回答"库还读得出来吗"，返回值不用。
                    self._store.read_turns(health.turn_hi, health.turn_hi)
                    # 再取 P9 的证据。这一步**必须**用组合入口（正文 + 指纹一次取完），
                    # 见 ``_foreign_signature`` 里对 P10 的说明。
                    foreign = self._check_foreign_driver(health.turn_hi)
                    if foreign is not None:
                        degraded = True
                        degrade_reason = pick_reason(degrade_reason, foreign.value)
            except Exception as exc:  # noqa: BLE001 - 探针只做健康判断
                code, detail = classify(exc)
                # ★ 三个处置，按"这个异常说明了什么"分，不按"在哪捕获的"分（§10.4）。
                #
                # 曾经的写法是「任何异常都 ``_mark_unreadable``」—— 那等于把
                # ``_readable`` 当成"刚才出过错"的垃圾桶：一个 ``InvariantError``
                # （存储实现/压缩器算错了不变量，库本身可能完好）会让会话从此
                # 不再读历史，代价远超该付的。状态位只能有一个含义。
                if code is DegradeReason.INTERNAL:
                    # 缺陷，不是环境。处置与 ``_try_boundary`` 的 INTERNAL 分支**一致**：
                    # 锁存压缩位（此后不再压缩），但库照读、payload 照带旧 F
                    # （前缀不断）。不丢 plan/F：这个 plan 是本进程内存里的对象，
                    # 它没有坏，坏的是刚才那次读取的实现。
                    self._compaction_blocked = DegradeReason.INTERNAL
                    self._note_degraded_mode(DegradeReason.INTERNAL)
                    verdict = "存储读取触发了不变量失败（压缩停用，库照读）"
                elif code in (DegradeReason.STORE_CORRUPT, DegradeReason.STORE_IO):
                    # 只有这两个码是"库读不出来"的证据（F1 / F3）。
                    self._mark_unreadable(code)
                    if code is DegradeReason.STORE_CORRUPT:
                        # F1：跳过存储，用进程内缓冲装配（无 F → R = 缓冲内全部轮次）
                        self._plan = None
                        self._frozen = None
                    verdict = "存储读取失败，后续仅用进程内缓冲"
                else:
                    # 剩下的码（``disk_full`` 等）不是"读不出来"的证据：读不需要写盘，
                    # 磁盘满不该让会话停读历史。只登记原因，``_readable`` 不动，
                    # 下一轮照旧探一次 —— 探针本来每轮都跑，不需要替它提前定案。
                    self._note_degraded_mode(code)
                    verdict = "存储读取失败（原因未指向库损坏，下一轮继续探）"
                # F3（磁盘满/只读）：payload 已与写盘解耦，不影响 F
                self._record(code, "第 %d 轮：%s：%s" % (index, verdict, detail),
                             fallback="buffer_only")
                degraded = True
                degrade_reason = pick_reason(degrade_reason, code.value)

        # 会话已锁存的原因（``_degraded_mode``）与本轮原因合并。
        #
        # 判据是**可逆性**，不是「这条原因是谁记的」：不可逆的那条下一轮仍然成立，
        # 可逆的那条下一轮自己就没了。所以 `missing_assistant`（下一轮就消失）盖住
        # `concurrent_driver`（永不消失）等于**最该修的那条从报告里消失**，而所有
        # 断言仍然是绿的 —— 只有这一个字段能拆穿它。
        #
        # 同层（都可逆 / 都不可逆）保留第一个，所以这条合并不改变既有行为。
        # 见 ``policy.pick_reason`` 与 ``IRREVERSIBLE_REASONS``。
        if self._degraded_mode is not None:
            degraded = True
            degrade_reason = pick_reason(degrade_reason, self._degraded_mode.value)

        # —— 5. 用**旧 plan** 探一次，得到 estimate（§5.1 第 3 步） ——
        probe = assemble(AssemblyInputs(
            system=self._system,
            frozen=self._frozen,
            turns=self._window(self._plan.r_first_turn if self._plan else 1),
            user_text=escaped,
            counter=self._counter,
            policy=self._policy,
            boundary=False,
            cold_start=(index == 1),
            degraded=degraded,
            degrade_reason=degrade_reason,
        ))
        estimate = probe.token_count

        # —— 4. 边界判定 ——
        reason = self._decide_boundary(index, estimate)
        if self._suppress_compact and reason is not BoundaryReason.NONE:
            # F9 的粘性：库空 → 放弃压缩，直到下一次 attribute() 成功。
            reason = BoundaryReason.NONE
        boundary = False
        if reason is not BoundaryReason.NONE:
            boundary, extra_degraded, extra_reason = self._try_boundary(turn, reason, estimate)
            degraded = degraded or extra_degraded
            degrade_reason = pick_reason(degrade_reason, extra_reason)

        # —— 8. 装配（纯函数区） ——
        self._phase = Phase.ASSEMBLING
        r_first = self._plan.r_first_turn if self._plan else 1
        payload = assemble(AssemblyInputs(
            system=self._system,
            frozen=self._frozen,
            turns=self._window(r_first),
            user_text=escaped,
            counter=self._counter,
            policy=self._policy,
            boundary=boundary,
            cold_start=(index == 1),
            degraded=degraded,
            degrade_reason=degrade_reason,
        ))

        # —— 断言（I1 的守护；仅 strict 模式） ——
        if (
            self._policy.strict_invariants
            and self._prev_payload is not None
            and not boundary
            and not payload.degraded
            and not self._prev_payload.degraded
        ):
            assert_prefix_stable(self._prev_payload, payload)

        # —— 6. 登记本轮（内存为准，写盘是旁路） ——
        self._turn_log.append(turn)
        self._next_index = index + 1
        # 写盘是**旁路**：它失败了也不回滚正文（§10.2）。但它返回的**原因**必须落到
        # 本轮的元数据上 —— 见下面 F4 之后那一段（§10.7）。
        write_reason = self._persist_turn(turn)

        # —— F4：软超预算照发，绝不截断用户输入 ——
        if payload.token_count > self._policy.budget_tokens:
            self._record(DegradeReason.BUDGET_OVERFLOW,
                         "第 %d 轮 payload %d token 超过预算 %d：软超预算照发，不截断用户输入"
                         % (index, payload.token_count, self._policy.budget_tokens),
                         fallback="buffer_only")
            # §10.5 的最后一格：**记了事件就必须同时标在 payload 上**。
            # 只记事件不标 payload，是"报告里有一条降级、而调用方一无所知" ——
            # 两边对不上，且没有任何一层会因为不一致而报错。
            #
            # 为什么可以事后 replace：`degraded` / `degrade_reason` 只进
            # `assembler.describe_payload()`（模块级函数，日志摘要），
            # **不参与正文渲染**，所以替换不改变一个字节。
            # 顺带：这也在 §10.5 那张"不许漏标"的名单里 —— 它明确写了
            # 「不产生带 degraded payload 的只有 F5 与 F14」，F4 不在其中。
            payload = replace(
                payload,
                degraded=True,
                degrade_reason=pick_reason(degrade_reason, DegradeReason.BUDGET_OVERFLOW.value),
            )

        # —— 写盘期出口（F16 的第 ① 条）：**当轮也要标**（§10.7） ——
        #
        # 曾经的写法是「本轮 payload 已定稿，从**下一轮**起才标」。前半句是事实，
        # 后半句不成立 —— 上面 F4 那段就是同一个函数、同一个变量上的事后 replace。
        #
        # 为什么不能只靠"下一轮会标"：`store_writable` 是**弱可观测面**，它等于
        # ``_writable and _readable``，而 F1 / F3 / F16 / 压缩器缺陷**全都**让它变
        # False。运维看到它只会去查磁盘，而真相可能是"这个 session 有两个驱动"。
        # 判据：**弱可观测面可以存在，但不能是唯一的那个。**
        #
        # 为什么可以事后补标：与 F4 同一条理由 —— 这两个字段只进
        # `assembler.describe_payload()`，不进 `Payload.to_bytes()`。
        # 为什么用 `pick_reason` 而不是直接赋：本轮可能已经有更不可逆的原因，
        # 直接赋会把它盖掉（`IRREVERSIBLE_REASONS`）。
        if write_reason is not None:
            payload = replace(
                payload,
                degraded=True,
                degrade_reason=pick_reason(payload.degrade_reason, write_reason.value),
            )

        self._phase = Phase.AWAIT_REPLY
        self._last_tokens = payload.token_count
        self._peak_tokens = max(self._peak_tokens, payload.token_count)
        self._prev_payload = payload
        return payload

    def observe(self, text: str, *, role: Role = Role.ASSISTANT) -> None:
        """登记本轮回复（assistant/tool）。失败只记录降级事件，不抛。"""
        try:
            if self._phase is Phase.CLOSED:
                return
            if not self._turn_log:
                self._record(DegradeReason.INTERNAL,
                             "observe() 时还没有任何轮次：回复无处挂载", fallback="buffer_only")
                return
            escaped, was_escaped = ensure_utf8(text, "助手回复")
            if was_escaped:
                self._record(DegradeReason.ENCODING_ERROR,
                             "回复含孤立代理项，已用 backslashreplace 转义", fallback="buffer_only")
            current = self._turn_log[-1]
            updated = current.with_reply(Message(role=role, text=escaped))
            self._turn_log[-1] = updated
            self._persist_turn(updated)
            self._phase = Phase.READY
        except Exception as exc:  # noqa: BLE001 - A4：observe 永不抛
            reason, detail = classify(exc)
            self._record(reason, "observe() 失败：%s" % detail, fallback="buffer_only")

    def attribute(self, delta_lines: Sequence[str]) -> MergeStat | None:
        """调用方（模型）给出的 T/P/R/E 增量。校验+归并+持久化。

        * 增量非法 → 抛 ``AttributionError``（调用方的程序错误，应当可见）；
        * 存储失败 → 不抛，记降级事件，返回 ``None``。

        归因只影响**下一个边界**的 F，不影响当前轮的 payload（I3）。
        所以"归因质量"不会污染当轮测量 —— 这是把 SDK 做成"记账"而非"理解"的直接收益。
        """
        if self._phase is Phase.CLOSED:
            raise LatticeError("会话已关闭（close() 之后不能再 attribute）")

        ops = parse_delta(delta_lines)  # 非法 → AttributionError，穿透
        if not ops:
            return MergeStat(changed=False)

        if self._writes_halted:
            # F16：本进程已彻底停写。**连 begin(write=True) 都不发**——判据是
            # "不再产生写事务"，不是"不再抛异常"；只吞异常不停写，等于每轮都去撞
            # 一次 ``UNIQUE(session_id, generation)``，而两次驱动的内容已经在交错。
            # 返回 None 而不是抛：与其它存储失败同形（A4 的"存储故障不改调用方
            # 契约"），真正需要改进程模型的那句话在 ``degradation_log()`` 里。
            return None

        turn_index = max(1, self._next_index - 1)
        try:
            with self._store.begin(write=True) as txn:
                stat = txn.apply_delta(delta_lines)
                txn.commit()
        except AttributionError:
            # 结构错误（例如 P 行前面没有 T 行）是**调用方的程序错误**，不是环境故障。
            # 它必须穿出去：把它伪装成"存储失败"会让模型永远拿不到那句
            # "增量必须按 T → P → R 的层级顺序书写"，而只是每轮悄悄少几条记忆。
            raise
        except Exception as exc:  # noqa: BLE001 - 存储失败不抛
            reason, detail = classify(exc)
            # 出口前缀：`attribute()` 里没有 payload 可标（它不在 ``send`` 里），
            # 所以这里的 ``_halt_writes`` / ``_note_degraded_mode`` 只能靠**下一轮**
            # 的 payload 显形。前缀让"这一轮为什么停写"在事件流水里可归因。
            self._record(reason, "[src=attrib] attribute() 落盘失败：%s" % detail,
                         fallback="buffer_only")
            if isinstance(exc, SessionConflictError):
                self._halt_writes(reason)
                return None
            # 与 ``_persist_turn`` 同一条判据（这是它的**同形状位置**，扫出来一起改）：
            # 写路径上任何失败都要落到写健康位，不许"只记一条事件"。只对
            # ``StoreIOError`` 动状态位的写法会让 ``StoreCorruptError`` 变成"报告里有
            # 一条、状态里没有"，下一次成功的 ``snapshot()`` 就把标记清掉。
            self._writable = False
            self._note_degraded_mode(reason)
            return None

        self._writable = True
        self._clear_degraded_mode_if_healthy()
        self._suppress_compact = False   # 库里有内容了，压缩重新可用
        self._touch(ops, turn_index)
        return stat

    def _touch(self, ops: Sequence, turn_index: int) -> None:
        """维护 ``last_touch``：path → 最近被提及的轮次。

        只在边界那一刻被快照读取，因此**不破坏 I3**（F 只在边界读它），
        边界内不再变化 → 不破坏 I1（§8.4）。

        解析失败（快照读不到）不是错误：``last_touch`` 只是相关度的一个信号，
        丢了它只会让选取退化成"只看文本命中"，不会产出错误内容。
        """
        try:
            snap = self._store.snapshot()
        except Exception:  # noqa: BLE001
            return
        topic_by_name = {normalize(t.name): t for t in snap.topics}
        current_topic = None
        for op in ops:
            if op.kind.value == "T":
                current_topic = topic_by_name.get(normalize(op.text))
                if current_topic is not None:
                    self._last_touch[current_topic.path] = turn_index
                continue
            if current_topic is None:
                continue
            for purpose in current_topic.purposes:
                if normalize(purpose.name) == normalize(op.text):
                    self._last_touch["T%d/P%s" % (current_topic.num, purpose.num)] = turn_index
                    break
            else:
                if op.kind.value in ("R", "E"):
                    # 挂在当前事物下的任一目的：都算"这个事物被动过"
                    self._last_touch[current_topic.path] = turn_index

    # -- 显式控制 ---------------------------------------------------------
    def request_focus(self, topic_paths: Sequence[str]) -> None:
        """点名某些子树在**下一个边界**必须进 F。当前边界内不生效。

        为什么"下个边界才生效"：让它在当前边界内生效就必须重写 F，
        而 F 在边界之间不可变（I3 / A2）—— 重写 F 等于把当前轮的缓存全部作废，
        且没有任何报错。把生效点推到边界，代价只是"晚一轮"。
        """
        for raw in topic_paths:
            path = normalize(raw)
            if path and path not in self._focus:
                self._focus.append(path)

    def force_compact(self, *, reason: str = "manual") -> CompactionEvent | None:
        """显式触发一次边界。返回 ``CompactionEvent``，失败返回 None（不抛）。

        ``reason`` 是给人看的标签；对外的 ``BoundaryReason`` 恒为 ``FORCED``。
        边界轮取「下一轮」（``index = 当前下一轮号``），``user_text`` 为空 ——
        因此 T6 的相关度查询为空，只有 ``request_focus()`` 点名的子树能进 F。
        这是刻意的：手动压缩没有"本轮用户消息"可当查询，编一个出来就是造假。
        """
        try:
            index = max(1, self._next_index)
            at_turn = Turn(index=index, user_text="")
            self._last_event = None
            boundary, _degraded, _reason = self._try_boundary(
                at_turn, BoundaryReason.FORCED, estimate=None)
            if not boundary:
                return None
            return self._last_event
        except Exception as exc:  # noqa: BLE001
            reason_code, detail = classify(exc)
            self._record(reason_code, "force_compact(%s) 失败：%s" % (reason, detail),
                         fallback="buffer_only")
            return None

    def inspect(self, *, topics: Sequence[str] | None = None) -> StoreSnapshot:
        """只读旁路：给调用方/调试用，**不参与装配**。按签名抛 ``StoreError``。

        ``topics`` 过滤只裁剪键树视图；``snapshot_id`` 仍然描述 S+F（P6），
        不随过滤变化 —— 它回答的是「这是哪一份冻结状态」，不是「这份视图里有什么」。
        """
        snap = self._store.snapshot()
        if topics is None:
            return snap
        wanted = {normalize(t) for t in topics}
        kept = tuple(
            t for t in snap.topics
            if t.path in wanted or normalize(t.name) in wanted
        )
        plan = snap.plan
        return StoreSnapshot(
            snapshot_id=_snapshot_id(
                schema=(plan.schema if plan is not None else FMT_SCHEMA),
                system_sha16=(plan.system_sha16 if plan is not None
                              else (snap.system.sha16 if snap.system is not None else "")),
                frozen_sha16=(plan.frozen_sha16 if plan is not None else ""),
            ),
            rev=snap.rev,
            topics=kept,
            turn_lo=snap.turn_lo,
            turn_hi=snap.turn_hi,
            plan=plan,
            system=snap.system,
            event_count=snap.event_count,
            top_text=snap.top_text,
            scope=snap.scope,
        )

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> SessionStats:
        return SessionStats(
            turns=max(0, self._next_index - 1),
            boundaries=self._boundaries,
            degradations=len(self._degradations) + self._degradation_dropped,
            store_writable=self._writable and self._readable,
            last_payload_tokens=self._last_tokens,
            peak_payload_tokens=self._peak_tokens,
        )

    def degradation_log(self) -> tuple:
        return tuple(self._degradations)

    def close(self) -> None:
        """尽力 flush；不抛。降级事件打到 stderr（**不写文件**：磁盘满时别再制造 IO）。"""
        try:
            if self._degradations:
                try:
                    print("[ctxlattice] 降级事件 %d 条（最旧 %d 条已丢弃）："
                          % (len(self._degradations), self._degradation_dropped),
                          file=sys.stderr)
                    for record in self._degradations:
                        print("[ctxlattice]   第 %d 轮 %s %s"
                              % (record.at_turn,
                                 getattr(record.reason, "value", record.reason),
                                 record.detail), file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._store.close()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._phase = Phase.CLOSED

    # -- 内部：边界 -------------------------------------------------------
    def _decide_boundary(self, turn_index: int, estimate: int) -> BoundaryReason:
        """判定顺序固定（§9.1）：COLD_START → PLAN_INVALID → INTERVAL → PRESSURE → NONE。

        ``PLAN_INVALID`` 只看**四条「plan 内部与会话常量是否自洽」**（§9.1 R1–R4）：

        ==========  ============================================  ==================
        校验         失败含义                                       本实现
        ==========  ============================================  ==================
        R1           ``frozen_sha16 == sha16(frozen_text)``          落库的 F 被改坏
        R2           ``system_sha16 == system.sha16``                S 变了
        R3           ``policy_digest == policy.digest()``            参数变了
        R4           ``counter_id == counter.counter_id``            计数口径变了
        ==========  ============================================  ==================

        **``plan.store_rev`` 与 ``plan.snapshot_id`` 不参与判定。** 这是 A7 的第 5 个
        实例，也是这套设计里最贵的一个坑：边界之间每调一次 ``attribute()``，
        ``rev`` 与 ``snapshot_id`` 都会变；若把它们当闸门，则**每一轮**都判
        ``PLAN_INVALID`` → 每轮重压 → 每轮全价轮。它不报错、不崩溃，只是成本静默
        翻几倍。F 的意义恰恰是冻结在过去的某个内容状态上，"与当前不一致"是设计
        意图，不是故障。

        ``plan.schema != FMT_SCHEMA`` 也不在这里判：schema 漂移由迁移器负责
        （STORE-SCHEMA §7：``user_version > CURRENT`` 拒绝打开），不该退化成
        每轮的边界判定。
        """
        plan = self._plan
        if not self._readable:
            # F1：存储已不可信 → 放弃压缩（R 段继续增长，行为退化为 A 臂）。
            # 不在这里返回 COLD_START：那会每轮都试一次压缩，每轮再多记一条降级。
            return BoundaryReason.NONE
        if self._compaction_blocked is not None:
            # 压缩器产出了自相矛盾的 plan（``InvariantError``，实现缺陷）：
            # 同一个压缩器下次还会算错，所以不再尝试。库与写入路径都还健康 ——
            # 这里只停压缩，不停读、也不停写。
            return BoundaryReason.NONE
        if plan is None:
            # 建过 plan 但现在没有（冻结块被改坏）→ PLAN_INVALID；
            # 从未建过 → COLD_START。第一轮永远是后者。
            return BoundaryReason.COLD_START if not self._ever_planned \
                else BoundaryReason.PLAN_INVALID
        if self._frozen is None or sha16_text(plan.frozen_text) != plan.frozen_sha16:
            return BoundaryReason.PLAN_INVALID            # R1
        if plan.system_sha16 != self._system.sha16:
            return BoundaryReason.PLAN_INVALID            # R2
        if plan.policy_digest != self._policy.digest():
            return BoundaryReason.PLAN_INVALID            # R3
        if plan.counter_id != self._counter.counter_id:
            return BoundaryReason.PLAN_INVALID            # R4
        return self._policy.boundary_due(plan, turn_index, estimate)

    def _try_boundary(
        self,
        at_turn: Turn,
        reason: BoundaryReason,
        estimate: int | None,
        *,
        force: bool = False,
    ) -> tuple[bool, bool, str | None]:
        """尝试执行一次边界。返回 ``(是否成功, 是否降级, 降级原因)``。**不抛。**"""
        if self._writes_halted:
            # F16：已彻底停写 → 连快照都不取（快照是读，但紧接着就是写事务，
            # 而"不再发起写事务"是这条契约的全部内容）。本轮的降级原因照旧上报，
            # 让调用方每一轮都看得见"这个会话已经不能压缩了"。
            self._phase = Phase.DEGRADED
            return False, True, DegradeReason.CONCURRENT_DRIVER.value
        self._phase = Phase.COMPACTING
        snapshot = None
        try:
            snapshot = self._store.snapshot()
            self._last_snapshot_id = snapshot.snapshot_id
            # 读得回来了 → 退出"仅缓冲"模式。边界随后会重建 plan/F，payload 回到
            # 设计路径；但只有读侧**写侧都健康**才允许清标记：写不进时清掉就是
            # 反向漏标（用户从此每轮都看不到"库进不去"这件事）。
            self._readable = True
            self._clear_degraded_mode_if_healthy()
        except Exception as exc:  # noqa: BLE001
            code, detail = classify(exc)
            self._mark_unreadable(code)
            self._record(code, "边界前取快照失败：%s" % detail, fallback="buffer_only")
            self._phase = Phase.DEGRADED
            return False, True, code.value

        # F9：库为空 → 放弃压缩（宁可不压，也不丢信息）
        if not snapshot.topics and self._policy.on_empty_store == "no_compact":
            if at_turn.index <= 1:
                # 第 1 轮没有任何历史可丢：压与不压完全等价，此时记一条降级
                # 会把"每一段新会话的第 1 轮"都标成 degraded，让报告里的
                # 降级轮数变成一个恒等于"会话数"的假指标。静默跳过即可。
                self._phase = Phase.READY
                return False, False, None
            self._record(DegradeReason.EMPTY_STORE_NO_COMPACT,
                         "存储里没有任何事物节点：放弃压缩，R 段继续增长（行为退化为 A 臂）",
                         fallback="buffer_only")
            self._plan = None
            self._frozen = None
            self._suppress_compact = True
            self._phase = Phase.DEGRADED
            return False, True, DegradeReason.EMPTY_STORE_NO_COMPACT.value

        if estimate is None:
            estimate = self._counter.count(self._system.text)

        try:
            result = compact(
                store=self._store,
                snapshot=snapshot,
                turn_log=self._turns_before(at_turn.index),
                at_turn=at_turn,
                reason=reason,
                policy=self._policy,
                counter=self._counter,
                system=self._system,
                focus=tuple(self._focus),
                last_touch=dict(self._last_touch),
                tokens_before=estimate,
            )
        except FreezeError as exc:
            # F11：中止本次压缩，沿用旧 F（存储未改变，I1 不受影响）
            self._record(DegradeReason.FREEZE_ABORT,
                         "第 %d 轮：%s" % (at_turn.index, exc), fallback="buffer_only")
            self._phase = Phase.READY
            return False, True, DegradeReason.FREEZE_ABORT.value
        except Exception as exc:  # noqa: BLE001
            code, detail = classify(exc)
            if isinstance(exc, SessionConflictError):
                # F16：压缩期撞到 plans 的唯一键 —— 这是"另一个驱动"的签名，
                # 不是库损坏。处置是彻底停写；这里**不动 I1**：本轮沿用旧 F
                # （payload 与上一轮同构、前缀不断），只是会话从此不再压缩。
                self._halt_writes(code)
            elif isinstance(exc, StoreIOError):
                self._writable = False
                self._note_degraded_mode(code)
            elif code is DegradeReason.STORE_CORRUPT:
                # 压缩期摸到损坏 —— 按 §10.4「分类以异常类型为键、不以捕获位置
                # 为键」，这**就是** F1，处置必须与探针路径（``_mark_unreadable``）
                # 逐字一致：丢掉 plan/F，此后仅用缓冲装配。
                # 不这么做的话，手上这份 plan 已经不可信却还留着，下一轮会先按旧
                # plan 装配一次（发出一个"看起来正常"的 payload），再被探针否掉 ——
                # 用户看到的是一轮正常的全价请求，而库其实是坏的。
                self._mark_unreadable(code)
                self._plan = None
                self._frozen = None
            elif code is DegradeReason.INTERNAL:
                # 压缩器自己产出了自相矛盾的 plan（``InvariantError``：``plans`` 的
                # CHECK 违反等）。这是**实现缺陷**，不是环境问题：
                #
                # * 不能归 F1 —— 库是好的，把它按"读不出来"处理会让会话连历史都
                #   不再读，远超该有的代价；
                # * 不能归 F16 —— 那是"怪调用方"，而正确答案在 compression 的
                #   ``r_first_turn`` / ``generation`` 计算里；
                # * 也不能只记一条事件就继续 —— 缺陷是**系统性**的，同一个压缩器
                #   下次还算错，会每 N 轮刷一条事件把 1024 条环形缓冲刷满，根因被
                #   自己的重复事件挤出去。
                #
                # 所以锁存一个独立状态位：此后不再尝试压缩（``_boundary_reason``
                # 直接返回 NONE），但 ``store_writable`` **保持 True**。
                self._compaction_blocked = DegradeReason.INTERNAL
                self._note_degraded_mode(DegradeReason.INTERNAL)
            # ``[src=compaction]``：出口的**机读**坐标。同一个 ``reason`` 可能来自
            # 三个出口（压缩期 / 写盘 / 读探针），中文散文区分不了，也会随改字失效。
            self._record(code, "[src=compaction] 第 %d 轮压缩失败：%s" % (at_turn.index, detail),
                         fallback="buffer_only")
            self._phase = Phase.READY
            return False, True, code.value

        self._adopt(result)
        self._focus = []          # 点名已消费
        self._boundaries += 1
        self._writable = True
        self._clear_degraded_mode_if_healthy()
        self._last_event = result.event
        self._phase = Phase.READY
        return True, False, None

    def _adopt(self, result: CompactionResult) -> None:
        """采纳新 plan，并**从 plan 重建 F 外壳**（而不是留着刚算出来的那个对象）。

        差异只有 ``tier_stats``：装配期它必须是空的（tier 记账属于
        ``CompactionEvent``）。统一的取数路径让「本进程刚压完」与「进程重启后
        从库里恢复」两种情形走同一条代码 —— 否则"重启前后 F 字节一致"这件事
        就只能靠运气。
        """
        self._plan = result.plan
        self._frozen = frozen_from_plan(result.plan)
        self._ever_planned = True

    # -- 内部：缓冲 / 持久化 / 降级记录 -----------------------------------
    def _window(self, r_first: int) -> tuple[Turn, ...]:
        return tuple(t for t in self._turn_log if t.index >= r_first)

    def _turns_before(self, index: int) -> tuple[Turn, ...]:
        return tuple(t for t in self._turn_log if t.index < index)

    def _persist_turn(self, turn: Turn) -> DegradeReason | None:
        """尽力持久化，返回**这一轮写盘失败的原因**（成功 / 无新故障 → ``None``）。

        返回值是**最小接口**：本函数拿不到 payload，也不改 payload。调用方（``send``）
        拿到原因后在**同一轮**的 payload 上补标 —— 判据是
        「正文不可回滚；元数据必须对齐」，不是"谁先算出来的"（§10.7）。

        返回 ``None`` 的只有两种情况：写成功了，或者**早就停写/不可写**
        （``_writes_halted`` / ``_writable=False``）。后者的原因已经在
        ``_degraded_mode`` 里锁存，``send`` 开头的 ``degraded = self._degraded_mode
        is not None`` 每一轮都会把它带上 —— 这里再返回一次只会让"本轮新发生的故障"
        与"历史状态"混成同一个值。

        失败时**绝不回滚已返回的 payload**（§10.2）。

        判据（团队定案）：**事件是流水，状态是快照；任何一条事件，要么改一个状态位，
        要么它就不该被写。**

        所以这里**没有**"记一条事件就完事"的分支：写路径上任何失败都落到写健康位
        （``_writable = False``），三种失败只是原因不同：

        * ``SessionConflictError`` —— P9 被违反，动作升级为**彻底停写**
          （``_halt_writes``：连写事务都不再发起）；
        * ``StoreIOError`` / ``StoreCorruptError`` / ``InvariantError`` / 其它 ——
          这一次写没成，此后不再尝试写盘，原因如实登记。

        曾经的写法只对前两种动状态位，第三种只 ``_record()``，后果是**"降级为真、
        状态为假"**：事件里有 ``store_corrupt``，而下一轮 ``snapshot()`` 一成功就把
        ``_degraded_mode`` 清掉了（``_readable`` 一直是 True），于是每一轮的
        ``degraded`` 都是 False —— 用户看到的是一个全绿的会话，而它的库从来没写进去过。
        这正是 §10.5 要堵的形状，而且比"完全没记"更难发现。

        为什么**不动** ``_readable``：写失败证明的是"写不进去"，不是"读不出来"。
        读侧由探针独立判定 —— 同一个异常若真来自读侧，探针下一轮会自己判一次
        （它有自己的处置：丢 plan/F），不需要在这里替它猜。

        为什么每条事件的 ``detail`` 都带 ``[src=write]``：出口不止一个（压缩期
        ``[src=compaction]``、读探针 ``[src=probe]``），而它们记的可能是**同一个**
        ``reason``。只靠中文散文区分，散文改一次就断；前缀是给机器读、给 grep 读的。
        """
        if not self._writable or self._writes_halted:
            return None
        try:
            # part_tokens 是 ``turn.token_count`` 列（v_turn_boundary 的算料）。
            # 顺手算出来而不是留 0：留 0 会让那张视图永远报「每轮 0 token」，
            # 而"看起来有数"的假数据比缺列更难发现。
            part_tokens = [self._counter.count(m.text) for m in turn.messages()]
            with self._store.begin(write=True) as txn:
                txn.put_turn(turn, part_tokens=part_tokens)
                txn.commit()
        except Exception as exc:  # noqa: BLE001
            code, detail = classify(exc)
            if isinstance(exc, SessionConflictError):
                # F16：同一 ``(turn_no, part)`` 的字节变了 —— 库里那一轮不是我发的。
                # 正文不变（写盘是旁路），但**当轮**就要标出来：本进程从此彻底停写
                # （``_writes_halted``），而"停写"这件事必须在这一轮的 payload 上看得见。
                #
                # 为什么不"只登记怀疑、等第二次观察"：F16 的二次确认是为**跨快照的
                # 内容比对**准备的（那条证据可能由读时序造成）。这一支在**写事务内**、
                # 不跨快照 —— 时序造不出它，单次即成立。证据强度不同，动作就该不同。
                self._halt_writes(code)
                self._record(code, "[src=write] 第 %d 轮入库冲突（P9 被违反，本进程此后停写）：%s"
                             % (turn.index, detail), fallback="buffer_only")
                return code
            # 其余一律落到写健康位：写失败就是写失败，原因分得清、状态位只有一个。
            self._writable = False
            self._note_degraded_mode(code)
            if isinstance(exc, StoreIOError):
                self._record(code, "[src=write] 写盘失败，会话转只读模式"
                                   "（不再尝试写盘）：%s" % detail,
                             fallback="buffer_only")
            else:
                self._record(code, "[src=write] 第 %d 轮入库失败，会话转只读模式"
                                   "（内存缓冲继续服务装配）：%s" % (turn.index, detail),
                             fallback="buffer_only")
            return code
        return None

    def _check_foreign_driver(self, hi: int) -> DegradeReason | None:
        """P9 的**唯一**检出口：库里第 n 轮的内容不是我发的第 n 轮 → F16。二次确认。

        为什么只剩这一条（原先还有一条"捕获 ``plans`` 唯一键"）：那条**真阳性为 0**。
        ``put_plan`` 由 ``BEGIN IMMEDIATE`` 开启，写锁从 BEGIN 持有到 COMMIT，
        预检与 INSERT 处在同一个排他区间内，另一驱动插不进来；能撞到的只有 CHECK /
        外键，两者都是实现缺陷。把它译成 F16 等于"用一个抓不到东西的检查冒充检出"，
        反而让人以为 P9 已有防护，不去做下面这条真正会命中的比对。

        为什么必须有这条：两个驱动若错开了 ``generation``，``plans`` 的唯一键
        **永不冲突**，但两段对话已经混进同一条 ``turn`` 链。那时唯一的信号就是
        "我发的 A，库里这一轮存的是 B"。``turn_seq`` 是串行发号的，所以**P3 的
        连续性断言照样通过**，别的检查抓不到。

        为什么比对**最近几轮**而不是只比 ``hi``：调用发生在"本轮 turn 已构造、
        尚未进缓冲"的时刻，本轮根本不在 ``_turn_log`` 里 —— 只读 ``hi`` 等于永远
        拿库和一份不含它的缓冲比，一个不一致都发现不了（实测：两个 Session 交替
        ``send`` 六轮，一次都没触发）。所以窗口取"我这边**已经交给装配**的、
        落在库区间内的最近 ``FOREIGN_PROBE_WINDOW`` 轮"，再加上一次怀疑过的那一轮。

        为什么必须二次确认（§10.6）：F16 的动作是**不可逆**的（本进程永久停写）。
        若把"跨快照读到的错位"当成 P9，代价是进程废掉，而它的表现与"话题稳定所以
        不需要压缩"一模一样 —— 没有任何信号提示判错了。所以同一处不一致要在**两次
        观察**上重现才成立；可逆降级（F1/F3/F11）才允许单次观测即生效。

        两个独立通道，任一重现即成立：
        * ``user_text`` 不一致 —— 一轮的用户消息落库后永不合法变化；
          （只比 ``user_text``，不比 ``replies``：回复条数可以合法落后，写盘是旁路）
        * ``content_hash`` 漂移 —— 同一个 ``(turn_no, part)`` 的指纹在两次观察间变了。
          首次见到即入账，所以改写会一直显形，第二次观察必然重现。

        ⚠️ P10：正文与指纹**一次调用取完**（``read_turns_with_hashes``），窗口内
        每一轮都出自同一快照。跨两次观察比指纹（检测改写）是合法且必须的 ——
        那不是配对，那是"同一把尺子量两次"。

        只读，不写，且不改缓冲：判定归本层，证据归存储层（§4.5）。
        """
        if self._writes_halted:
            return None

        mine = [t.index for t in self._turn_log if t.index <= hi]
        candidates = mine[-FOREIGN_PROBE_WINDOW:]
        if self._suspect_turn is not None and self._suspect_turn <= hi:
            # 上次怀疑过的那一轮必须**再读一次**，否则"同一处不一致"永远只有一次
            # 观测（窗口每轮在滑动，只读最新几轮等于放走了上一次的怀疑）。
            candidates = [self._suspect_turn] + candidates
        candidates = sorted({n for n in candidates if n >= 1})
        if not candidates:
            self._suspect_turn = None
            self._suspect_signature = None
            return None

        lo, hi2 = candidates[0], candidates[-1]
        stored_turns, hashes = self._store.read_turns_with_hashes(lo, hi2)
        stored_by_index = {t.index: t for t in stored_turns}
        mine_by_index = {t.index: t for t in self._turn_log}

        first: tuple[int, tuple] | None = None
        for turn_no in candidates:
            evidence = self._foreign_evidence(
                turn_no, stored_by_index[turn_no], hashes, mine_by_index.get(turn_no))
            if evidence is None:
                continue
            if self._suspect_turn == turn_no and self._suspect_signature == evidence:
                self._suspect_turn = None
                self._suspect_signature = None
                self._halt_writes(DegradeReason.CONCURRENT_DRIVER)
                self._record(
                    DegradeReason.CONCURRENT_DRIVER,
                    "[src=probe] 第 %d 轮在库里的内容与本进程缓冲不一致（连续两次观察成立）："
                    "同一 session_id 被两个驱动写。本进程此后停写，payload 照发"
                    % turn_no, fallback="buffer_only")
                return DegradeReason.CONCURRENT_DRIVER
            if first is None:
                first = (turn_no, evidence)

        if first is None:
            self._suspect_turn = None
            self._suspect_signature = None
        else:
            self._suspect_turn, self._suspect_signature = first
        return None

    def _foreign_evidence(
        self,
        turn_no: int,
        stored: Turn,
        hashes: "Mapping[tuple[int, int], str]",
        mine: Turn | None,
    ) -> tuple | None:
        """单轮外来证据。``None`` 表示这一轮对得上（或我这边还没有这一轮）。

        ``stored`` 与 ``hashes`` 必须来自**同一次** ``read_turns_with_hashes``。
        这不是洁癖：分两次读时，两次之间插进一次外部提交就会得到"快照 A 的正文 +
        快照 B 的指纹"，而那个组合既可能是真 P9、也可能纯粹是读时序 —— **在证据上
        无法区分**，而 F16 的动作不可逆（STORE-SCHEMA §10.3 / ARCHITECTURE §10.6）。
        """
        evidence: list = []
        if mine is not None and stored.user_text != mine.user_text:
            evidence.append(("text", stored.user_text))

        drifted: list[tuple[int, int]] = []
        for key in sorted(hashes):
            if key[0] != turn_no:
                continue
            value = hashes[key]
            seen = self._turn_hashes_seen.get(key)
            if seen is None:
                self._turn_hashes_seen[key] = value   # 只记第一次，改写才能一直显形
            elif seen != value:
                drifted.append(key)
        if drifted:
            evidence.append(("hash", tuple(drifted)))

        return tuple(evidence) or None

    def _halt_writes(self, reason: DegradeReason) -> None:
        """F16：探测到 P9 被违反 → **本进程此后彻底停写**。

        为什么不是"降级到只读再重试"：F1/F3 都是环境问题，等一等、换条连接、
        腾出磁盘就能好；而 P9 是**调用方的进程模型错了** —— 两个驱动各持一份
        ``Session`` 缓冲去写同一条轮次链，`turn_seq` 会串行发号（P3 的连续性断言
        照样通过！），两段对话被交错进同一条链。这时"重试"只会把交错写得更深。

        所以停写必须覆盖三处写入口（``_persist_turn`` / ``attribute`` /
        ``_try_boundary``），且判据是**不再发起写事务**：只吞异常不停写，等于每轮
        都去撞一次唯一键，日志里 1024 条环形缓冲会被同一个原因刷满。

        出口不止一个，而它们记的可能是**同一个** ``reason``。所以每处出口的事件都带
        机读前缀（§10.7）：

        | 出口 | 前缀 | 它回答的问题 |
        |---|---|---|
        | 压缩期 | ``[src=compaction]`` | 压缩写事务里撞的（号码算错 / 另一个驱动） |
        | 写盘 | ``[src=write]`` | 轮次入库撞的（同一 ``(turn_no, part)`` 换了字节） |
        | 归因 | ``[src=attrib]`` | ``attribute()`` 的写事务撞的 |
        | 读探针 | ``[src=probe]`` | 跨快照比对连成两次（唯一需要二次确认的那个） |

        ``tests/test_conflict.py::test_two_exits_detail_prefixes_differ`` 断言它们
        互不相同。没有这条断言，"两条出口"在报告里仍然是一个字 —— 只能靠中文散文
        分辨，而散文改一次就断。
        """
        self._writes_halted = True
        self._writable = False
        self._note_degraded_mode(reason)

    def _note_degraded_mode(self, reason: DegradeReason) -> None:
        """登记"会话已不在设计路径上"（§10.5）。**只记账，不记事件。**

        保留**第一次**的原因：库坏了之后又碰上磁盘满，根因仍然是"库坏了"，
        把它覆盖成 ``disk_full`` 会让报告里最该修的那条消失。

        为什么和 ``_record()`` 分开：``_record`` 是事件（检测到的那一刻记一次，
        1024 条环形缓冲），这里是**状态**（每轮 payload 都要带）。混在一起的后果
        是每轮补一条事件把缓冲刷爆，根因被挤出去 —— 比不记更坏。
        """
        if self._degraded_mode is None:
            self._degraded_mode = reason

    def _clear_degraded_mode_if_healthy(self) -> None:
        """读侧、写侧、压缩器**三者都**健康，才算回到设计路径。

        判据刻意不写成"某一次操作成功了"：``attribute()`` 写成功不代表读得出历史，
        边界快照读出成功也不代表写不进去，更不代表压缩器算得对。

        第三个合取项（``_compaction_blocked is None``）不是补丁：``degraded`` 的定义
        是"这一轮走的不是设计路径"，而一个永远不再压缩的会话**每轮**都不在设计路径上，
        即使库读写都正常。少了它，``_compaction_blocked`` 就变成一个不可观测的状态：
        行为退化了，报告全绿 —— 这正是这套设计里最贵的那个失败形状。

        一条可执行的自查判据（团队定案）：
        **一个状态位如果没有任何一条「用户可见的输出」依赖它，它就不是状态，是注释。**
        这里"用户可见的输出"就是 payload 的 ``degraded``。
        """
        if (self._readable and self._writable
                and self._compaction_blocked is None):
            self._degraded_mode = None

    def _mark_unreadable(self, reason: DegradeReason) -> None:
        """把会话钉在"读不出库"模式（F1），并让此后每一轮都如实标降级。

        为什么需要这个开关而不是只置 ``_readable=False``：``_readable`` 只影响
        "这一轮读不读库"，而 ``degraded`` 回答的是"这一轮的 payload 是哪条路径产出的"。
        后者一旦变成 buffer_only，就是**持续**事实 —— 它不会在下一轮自己恢复。
        同一件事用两个字段表达，是因为它们被读的地方不同（控制流 vs 报告）。
        """
        self._readable = False
        self._note_degraded_mode(reason)

    def _record(self, reason: DegradeReason, detail: str, *,
                fallback: str = "buffer_only") -> None:
        """登记一条降级事件。环形缓冲，超限丢最旧并计数（§10.3）。

        ``fallback`` 的语义是**这一轮 payload 的数据来源**，取值只能是
        STORE-SCHEMA ``event.fallback`` 的三个白名单值（DDL 有 CHECK，写第四种
        会被判成"存储损坏"，那正是错误分类最不该出错的地方）：

        * ``buffer_only`` —— 用进程内轮次缓冲装配。**正常路径也是它**：
          §5.1 第 2 步明确「不读存储」，R 段永远来自 ``Session._turn_log``。
          所以"没有降级"与"只用缓冲"在数据来源上是同一件事，区别在 ``reason``；
        * ``full_history`` —— F12：退到 S + 全量 turns + U（A 臂等价物）；
        * ``last_resort`` —— F13：S + U + 一行说明。

        把「严重度」编码进这个字段是个诱惑（比如加一个 ``"none"``），但那样
        "降级到哪"就答不出来了 —— 而报告要的正是后者。严重度由 ``reason`` 承担。
        """
        from .model import DegradationRecord

        record = DegradationRecord(
            at_turn=max(0, self._next_index - 1),
            at_tick=safe_tick(self._clock),
            reason=reason,
            detail=sanitize(detail, 200),
            snapshot_id=self._last_snapshot_id,
            fallback=fallback,
        )
        self._degradations.append(record)
        if len(self._degradations) > DEGRADATION_RING:
            drop = len(self._degradations) - DEGRADATION_RING
            del self._degradations[:drop]
            self._degradation_dropped += drop

    # -- 内部：紧急兜底 ---------------------------------------------------
    def _emergency(self, user_text: str, exc: BaseException) -> Payload:
        """F12 → F13：装配内部异常 → 全量历史 → 最后兜底。**绝不抛。**"""
        reason, detail = classify(exc)
        try:
            self._record(reason, "send() 内部异常，退到全量历史：%s" % detail,
                         fallback="full_history")
        except Exception:  # noqa: BLE001
            pass
        try:
            payload = full_history_payload(
                system=self._system,
                turns=tuple(self._turn_log),
                user_text=user_text,
                counter=self._counter,
                policy=self._policy,
                reason=reason,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            try:
                payload = buffer_only_payload(
                    system=self._system, turns=(), user_text=user_text,
                    counter=self._counter, policy=self._policy,
                    reason=reason, detail=detail)
            except Exception:  # noqa: BLE001
                payload = self._last_resort(user_text)

        # 兜底路径也要把本轮登记进缓冲，否则下一轮 R 段会缺一轮（有洞）。
        try:
            index = self._next_index
            escaped, _ = ensure_utf8(user_text, "兜底轮次")
            turn = Turn(index=index, user_text=escaped)
            self._turn_log.append(turn)
            self._next_index = index + 1
            self._persist_turn(turn)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._phase = Phase.DEGRADED
            self._last_tokens = payload.token_count
            self._peak_tokens = max(self._peak_tokens, payload.token_count)
        except Exception:  # noqa: BLE001
            pass
        return payload

    def _last_resort(self, user_text: str) -> Payload:
        """连 ``last_resort_payload`` 都失败时的最后一道：手工拼一个最小 Payload。"""
        try:
            return last_resort_payload(
                system=self._system, user_text=user_text, counter=self._counter)
        except Exception:  # noqa: BLE001
            from .model import BlockKind, make_block

            note = "[降级] 历史与计数均不可用\n"
            return Payload(
                blocks=(make_block(BlockKind.R, note, 0),
                        make_block(BlockKind.U, "user: %s\n" % user_text, 0)),
                counter_id="unknown",
                degraded=True,
                degrade_reason="%s: 手工兜底" % DegradeReason.INTERNAL.value,
            )

    # -- 诊断 -------------------------------------------------------------
    def describe(self) -> str:
        """一行状态摘要（日志用，不进 payload 字节）。"""
        snap = self._last_snapshot_id or "-"
        plan = ("gen=%d epoch=%d r_first=%d" % (
            self._plan.generation, self._plan.epoch_turn, self._plan.r_first_turn)
            if self._plan else "无 plan")
        return "phase=%s 轮次=%d snapshot=%s %s 边界=%d 降级=%d 可写=%s" % (
            self._phase.value, max(0, self._next_index - 1), snap, plan,
            self._boundaries, len(self._degradations), self._writable)


__all__ = ["BUILTIN_SYSTEM_SOURCE", "DEGRADATION_RING", "Phase", "Session",
           "make_system_prompt"]
