"""存储端口（协议）+ 纯内存参考实现。

这里只钉**行为契约**（ARCHITECTURE §4.5 的 P1–P9），schema 由 STORE-SCHEMA.md 定义。
``MemoryStore`` 是参考实现：纯内存，供 tests 与 bench 离线回放使用，
**行为必须与 SQLite 版逐点一致** —— 两版行为不一致时，bench 的数字就不可比，
而且差异不会以任何形式报错（一边是"能跑"，另一边也是"能跑"）。

9 条契约与它们的兑现方式：

| # | 契约 | 本文件怎么兑现 |
|---|---|---|
| P1 | ``snapshot()`` 是一致的只读视图 | 已提交状态以不可变 tuple 持有，事务只改副本，``commit()`` 一次性换引用 |
| P2 | ``commit()`` 原子；ENOSPC 整体回滚 | 换引用前不触碰已提交状态；抛错即弃副本 |
| P3 | 轮次连续无洞 | ``read_turns`` 条数 + 逐值两处断言；``snapshot`` 再做一次全库聚合断言 |
| P4 | ``rev`` 只在内容指纹真变化时 +1 | ``commit()`` 比较 ``body_key`` 前后值，不看"改了几行" |
| P5 | 遍历顺序恒为 (topic.num, purpose.num, 插入序) | 列表即顺序，不依赖任何哈希序 |
| P6 | ``snapshot_id`` 由 ``schema + system_sha16 + frozen_sha16`` 派生 | ``text.snapshot_id()`` 是纯派生函数，不另算 dump |
| P7 | 并发写不丢更新（SQLite 侧） | 内存版无并发，只保证"不丢已提交状态"；SQLite 版见 ``store_sqlite`` |
| P8 | ``plans`` 存 ``frozen_text``，按 generation 追加，取最大 generation，读时校验 sha16 | ``_Committed.plans`` 是全量 tuple；``snapshot()`` 取 generation 最大者并校验 |
| P9 | 同一 session 的装配只能单进程驱动 | 本层不阻止（拦不住），由 ``Session`` 的 docstring 与契约声明 |

端口上的两处**内部扩展**（Q1 允许「不要改签名，可加内部方法」）：

* ``put_measure()`` —— ``measure`` 表（STORE-SCHEMA 第 11 表）是 bench 的测点，
  ``put_turn(turn)`` 的签名里没有测量数据。它是**加**的方法，不改任何既有签名。
* ``atom_dir`` / ``base_dir`` —— ``E`` 节点的内嵌副本目录与 ``src`` 相对路径
  的基准目录。缺省为 None 时，``embed=True`` 与相对 ``src`` 会**明确报错/报缺失**
  而不是猜一个当前工作目录（猜 cwd 会让同一份库在两台机器上 fetch 到不同内容）。

依赖：``model`` / ``policy`` / ``text`` / ``attribution`` / ``errors`` / ``clock``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from . import attribution
from .clock import ClockPort, safe_iso
from .errors import SessionConflictError, StoreCorruptError, StoreError, StoreIOError
from .model import (
    AssemblyPlan,
    CompactionEvent,
    DegradationRecord,
    MergeStat,
    StoreSnapshot,
    SystemPrompt,
    Topic,
    Turn,
    bump_store_reads,
)
from .text import FMT_SCHEMA
from .text import body_key as _body_key_of
from .text import snapshot_id as _snapshot_id
from .text import sha16_text, sha256_hex

if TYPE_CHECKING:
    from .attribution import NumberAllocator
    from .model import Measure


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
@runtime_checkable
class StoreTxn(Protocol):
    def __enter__(self) -> "StoreTxn": ...

    def __exit__(self, et, ev, tb) -> bool: ...

    def apply_delta(self, lines: Sequence[str]) -> MergeStat: ...

    def put_turn(self, turn: Turn) -> None: ...

    def put_plan(self, plan: AssemblyPlan) -> int:
        """落一代 plan，**返回实际分配到的 generation**（本方法在写事务内分配）。

        ``plan.generation`` 在这里只是**候选号**：调用方按 snapshot 算出来的那个。
        真实号由本方法按 ``MAX(generation) + 1`` 分配（ARCHITECTURE §10.4），
        因为只有在写事务内分配，任意数量的驱动才不可能拿到同一个号。
        候选号只用于识别"重试提交同一份冻结字节"（同号 + 同字节 → 不新增行，返回原号）。

        ⚠️ 调用方**必须把返回值采纳回内存里的 plan**（``replace(plan, generation=…)``），
        否则下一次压缩会拿着旧号再算一遍，候选号与库里的号重新错开。
        """
        ...

    def put_system(self, prompt: SystemPrompt) -> None: ...

    def append_event(self, event: "CompactionEvent | DegradationRecord") -> None: ...

    def put_measure(self, measure: "Measure") -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class StorePort(Protocol):
    """存储端口。实现：``MemoryStore``（本文件）/ ``SqliteStore``（store_sqlite）。

    读侧只有**三个**入口，且后两个都委托到 ``read_turns_with_hashes``：
    正文与指纹在结构上不允许来自两个快照（P10）。
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def snapshot(self) -> StoreSnapshot: ...

    def read_turns(self, lo: int, hi: int) -> tuple[Turn, ...]: ...

    def read_turns_with_hashes(
        self, lo: int, hi: int
    ) -> tuple[tuple[Turn, ...], Mapping[tuple[int, int], str]]: ...

    def begin(self, *, write: bool = False) -> StoreTxn: ...


# ---------------------------------------------------------------------------
# 内存参考实现
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Committed:
    """一次 ``commit()`` 之后的**不可变**状态。事务只改自己的副本，提交时整体换引用。"""

    topics: tuple[Topic, ...]
    rev: int
    body_key: str
    top_text: str
    scope: str
    plans: tuple[AssemblyPlan, ...]
    system: SystemPrompt | None
    turns: tuple[tuple[int, Turn], ...]      # 已按 turn_no 升序；P3 断言依赖这一点
    events: tuple[object, ...]
    measures: tuple[object, ...]


class MemoryStore:
    """内存参考实现。

    **行为必须与 ``SqliteStore`` 逐点一致**：两版不一致时，bench 的数字不可比，
    而差异不会以任何形式报错（两边都"能跑"）。``tests/test_determinism.py``
    用同一段会话逐轮比对 ``sha16`` 来钉住这件事。
    """

    def __init__(
        self,
        *,
        store_id: str = "memory",
        top_text: str = "",
        scope: str = "",
        clock: "ClockPort | None" = None,
        session_id: str | None = None,
    ) -> None:
        self.store_id = store_id
        #: MemoryStore 一个实例就是一条 session（SQLite 版可以在同一个库里放多条）。
        self.session_id = session_id if session_id is not None else store_id
        self.clock = clock
        self._clock = clock
        self._opened = False
        #: 测试注入点：置一个异常对象，下次 ``commit()`` 就抛它（模拟 ENOSPC）。
        #: 内存版没有真实 IO，但"提交失败必须整体回滚"（P2）是端口契约，
        #: 没有这个抓手就只能靠 SQLite 版来测，而它要慢两个数量级。
        self.fail_commit_with: BaseException | None = None
        self._alloc_topic_last = 0
        self._alloc_purpose_last = 0
        self._state = _Committed(
            topics=(),
            rev=0,
            body_key=_body_key_of(top_text, scope, ()),
            top_text=top_text,
            scope=scope,
            plans=(),
            system=None,
            turns=(),
            events=(),
            measures=(),
        )

    # -- 生命周期 ---------------------------------------------------------
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    # -- 读 ---------------------------------------------------------------
    def snapshot(self) -> StoreSnapshot:
        bump_store_reads()
        st = self._state
        turn_nos = [n for n, _t in st.turns]
        _assert_turn_continuity(turn_nos)
        plan = _latest_plan(st.plans)
        if plan is not None and sha16_text(plan.frozen_text) != plan.frozen_sha16:
            raise StoreCorruptError(
                "第 %d 代计划落库的冻结块被改坏：plan.frozen_sha16=%s，正文实测 %s。"
                "按 P8 / R1，读取侧只校验最大 generation 那一行，不符即报损坏（不静默修）。"
                "静默重算会让 I1 断在没人看的地方"
                % (plan.generation, plan.frozen_sha16, sha16_text(plan.frozen_text)))
        system_sha16 = st.system.sha16 if st.system is not None else ""
        return StoreSnapshot(
            snapshot_id=_snapshot_id(
                schema=plan.schema if plan is not None else FMT_SCHEMA,
                system_sha16=system_sha16,
                frozen_sha16=plan.frozen_sha16 if plan is not None else "",
            ),
            rev=st.rev,
            topics=st.topics,
            turn_lo=turn_nos[0] if turn_nos else 0,
            turn_hi=turn_nos[-1] if turn_nos else 0,
            plan=plan,
            system=st.system,
            event_count=len(st.events),
            top_text=st.top_text or None,
            scope=st.scope or None,
        )

    def read_turns_with_hashes(
        self, lo: int, hi: int
    ) -> tuple[tuple[Turn, ...], Mapping[tuple[int, int], str]]:
        """一次读出**正文与指纹**（P10 的唯一安全入口）。见 ``SqliteStore`` 同名长注释。

        内存实现天然一致：只取一次 ``self._state`` 引用，之后不再回读状态 ——
        即使另一线程在中间提交，本方法看到的仍是同一份不可变快照。
        """
        bump_store_reads()
        if lo < 1:
            raise StoreError(
                "read_turns_with_hashes 的 lo 必须 ≥ 1（轮次号 1 起），实测 %d" % lo)
        if hi < lo:
            return (), {}
        table = dict(self._state.turns)
        out: list[Turn] = []
        for index in range(lo, hi + 1):
            turn = table.get(index)
            if turn is None:
                raise StoreCorruptError(
                    "轮次不连续：第 %d 轮缺失（请求区间 %d..%d，实际可读 %s）。"
                    "R 段依赖轮次连续，有洞即不可装配 —— 宁可报错也不猜一段历史"
                    % (index, lo, hi, _describe_range(sorted(table))))
            out.append(turn)
        expected = hi - lo + 1
        if len(out) != expected:
            raise StoreCorruptError(
                "read_turns 区间 %d..%d 应有 %d 轮，实测 %d 轮：轮次有洞"
                % (lo, hi, expected, len(out)))
        if [t.index for t in out] != list(range(lo, hi + 1)):
            raise StoreCorruptError(
                "read_turns 区间 %d..%d 的轮次号不是逐值出现：实测 %s"
                % (lo, hi, [t.index for t in out]))
        hashes: dict[tuple[int, int], str] = {}
        for turn in out:
            for part, message in enumerate(turn.messages()):
                hashes[(turn.index, part)] = turn_content_hash(message.text)
        return tuple(out), hashes

    def read_turns(self, lo: int, hi: int) -> tuple[Turn, ...]:
        """闭区间取轮次，有洞即 ``StoreCorruptError``。委托给组合入口，只留一条读路径。"""
        return self.read_turns_with_hashes(lo, hi)[0]

    # -- 写 ---------------------------------------------------------------
    def begin(self, *, write: bool = False) -> "_MemoryTxn":
        return _MemoryTxn(self, write=write)

    # -- 内部 -------------------------------------------------------------
    def _allocator(self) -> "NumberAllocator":
        return attribution.NumberAllocator(
            topic_last=self._alloc_topic_last,
            purpose_last=self._alloc_purpose_last,
        )

    def _absorb_allocator(self, alloc: "NumberAllocator") -> None:
        self._alloc_topic_last = alloc.snapshot_topic_last()
        self._alloc_purpose_last = alloc.snapshot_purpose_last()

    def _now_iso(self) -> str:
        return safe_iso(self._clock, "1970-01-01T00:00:00.000Z")


def _assert_turn_continuity(numbers: Sequence[int]) -> None:
    """P3 的第二处常开断言（STORE-SCHEMA §4.5）：

    ``MIN(turn_no) == 1 AND MAX(turn_no) == COUNT(DISTINCT turn_no)``。

    它兜的是「整库被外部删改」：``read_turns`` 只校验真正要用的那一段，
    而一段之外少了几轮，装配看不出来，直到某天有人把窗口开到那里。
    """
    if not numbers:
        return
    lo = min(numbers)
    hi = max(numbers)
    distinct = len(set(numbers))
    if lo != 1 or hi != distinct:
        raise StoreCorruptError(
            "轮次不连续：最小轮次 %d（应为 1），最大轮次 %d，去重计数 %d。"
            "整库聚合断言失败说明轮次被外部删改过 —— 宁可报错也不猜一段历史"
            % (lo, hi, distinct))


def _latest_plan(plans: Sequence[AssemblyPlan]) -> AssemblyPlan | None:
    """``ORDER BY generation DESC LIMIT 1`` 的等价物（R7）。

    刻意不用「epoch_turn 最大」：重压时 ``generation`` 递增而 ``epoch_turn``
    不一定单调（重建可能把边界往回挪），取 epoch 最大会把已压进冻结块的原文
    重新拉回窗口 —— 看起来只是"这次装配比较大"，没有任何断言会失败。
    """
    best: AssemblyPlan | None = None
    for plan in plans:
        if best is None or plan.generation > best.generation:
            best = plan
    return best


def _describe_range(numbers: Sequence[int]) -> str:
    if not numbers:
        return "（空）"
    gaps = []
    for prev, cur in zip(numbers, numbers[1:]):
        if cur != prev + 1:
            gaps.append("%d→%d" % (prev, cur))
    return "已存 %d..%d%s" % (
        numbers[0], numbers[-1], ("，断裂 " + " / ".join(gaps)) if gaps else "")


class _MemoryTxn:
    """内存事务：改副本，``commit()`` 一次性换引用（P1 + P2）。"""

    def __init__(self, store: MemoryStore, *, write: bool) -> None:
        self._store = store
        self._write = write
        base = store._state
        self._topics = list(base.topics)
        self._rev = base.rev
        self._body_key = base.body_key
        self._top_text = base.top_text
        self._scope = base.scope
        self._plan = base.plans
        self._system = base.system
        self._turns: dict[int, Turn] = dict(base.turns)
        self._events = list(base.events)
        self._measures: list[Measure] = list(base.measures)
        self._alloc = store._allocator()
        self._finished = False

    # -- 上下文协议 -------------------------------------------------------
    def __enter__(self) -> "_MemoryTxn":
        return self

    def __exit__(self, et, ev, tb) -> bool:
        if not self._finished:
            self.rollback()
        return False

    # -- 写操作 -----------------------------------------------------------
    def _require_write(self, what: str) -> None:
        if not self._write:
            raise StoreError(
                "只读事务不能执行 %s：请用 begin(write=True) 开启写事务" % what)

    def apply_delta(self, lines: Sequence[str]) -> MergeStat:
        self._require_write("apply_delta")
        ops = attribution.parse_delta(lines)
        new_topics, stat = attribution.apply_delta(
            self._topics, ops, alloc=self._alloc)
        new_key = _body_key_of(self._top_text, self._scope, new_topics)
        if new_key == self._body_key:
            # 内容指纹未变 → 不写盘、不 +rev（P4）。分配器也随之作废：
            # 让它泄漏一个号就是"号已发、行不存在"的永久空洞。
            return replace(stat, changed=False)
        self._topics = new_topics
        self._body_key = new_key
        return stat

    def put_turn(self, turn: Turn, *, part_tokens: Sequence[int] | None = None) -> None:
        """追加一轮。同一 ``(turn_no, part)`` 内容不同 → ``SessionConflictError``。

        ``part_tokens`` 对应 ``turn.token_count`` 列（STORE-SCHEMA 第 7 表），
        是 ``v_turn_boundary`` 的算料。内存版不保留它（端口上没有任何读路径
        暴露它，装配也永不读它），SQLite 版照写 —— 两边在**端口可观测行为**上
        完全一致，这正是"逐点一致"的定义。

        **为什么是 ``SessionConflictError`` 而不是 ``StoreCorruptError``**
        （与 ``store_sqlite._SqliteTxn.put_turn`` 同一段理由，两处必须逐字同向）：

        按 §10.4 的可达性判据 —— *只可能被缺陷撞到的分支指向缺陷（``InvariantError``），
        可能被正确输入撞到的分支指向调用方*。这一支**被正确输入撞得到**：两个
        ``Session`` 各持一份缓冲写同一条轮次链（P9 被违反）时，各自的第 2 轮用户
        消息内容不同，先到的那个已经落库，后到的必撞这一支。这是调用方的进程模型
        错了，不是库坏了 —— 译成 ``StoreCorruptError`` 会让运维去跑
        ``integrity_check``（那一定全绿），而真正的处置"这个 session 有两个驱动"
        永远不会被执行。

        反面参照：``plans`` 的唯一键**真阳性为 0**（号在写事务内分配，
        ``BEGIN IMMEDIATE`` 的锁从 BEGIN 持有到 COMMIT），那里的"撞"只可能是实现
        缺陷 —— 所以那支根本不该存在，而不是该抛这个异常。**同形状，相反结论，
        因为可达输入集相反。**
        """
        self._require_write("put_turn")
        existing = self._turns.get(turn.index)
        if existing is not None:
            # 幂等重放 OK；内容不同才是危险情形（上游分叉/纠正）——
            # 静默覆盖会让缓存命中率虚高，所以必须报错。
            if existing.user_text != turn.user_text:
                raise SessionConflictError(
                    "第 %d 轮的内容与已存记录不同：已存 %s，本次 %s。"
                    "一轮的用户消息落库后永不合法变化 —— 这通常意味着同一 "
                    "session 被两个驱动写（P9 被违反）；静默覆盖会让缓存命中率虚高"
                    % (turn.index, sha16_text(existing.user_text), sha16_text(turn.user_text)))
            # 回复是追加的：已有的 part 若内容变了同样是分叉。
            for pos, reply in enumerate(turn.replies):
                if pos < len(existing.replies):
                    old = existing.replies[pos]
                    if old.role != reply.role or old.text != reply.text:
                        raise SessionConflictError(
                            "第 %d 轮第 %d 条回复与已存记录不同（role %s→%s）："
                            "回复只许按 part 追加，改历史等于分叉"
                            % (turn.index, pos + 1, old.role.value, reply.role.value))
            if len(turn.replies) < len(existing.replies):
                # 端口契约是「回复只许按 part 追加」。这一份带回来的回复**比库里少**：
                # SQLite 版按 part 逐条写，已有 part 永远保留，所以对它来说这件事
                # 等于"什么都没发生"（只顺手更新 pinned）。内存版若照抄
                # ``self._turns[i] = turn``，会把已登记的回复**静默删掉** ——
                # 两后端在"回复变少"上给出相反的结果，而差异只在重启后显形
                # （装配读的是进程内缓冲，看不见这条）。所以这里对齐 SQLite：
                # 保留更全的那一份，只采纳 pinned。
                self._turns[turn.index] = replace(existing, pinned=turn.pinned)
                return
        self._turns[turn.index] = turn

    def put_plan(
        self,
        plan: AssemblyPlan,
        *,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
    ) -> int:
        """追加一代 plan，**返回实际分配到的 generation**（P8：追加，**永不覆盖**）。

        ``tokens_before`` / ``tokens_after`` 是 STORE-SCHEMA ``plans`` 的两个测量列；
        ``AssemblyPlan`` 里没有它们的载体（它们是测量字段，不是装配输入），
        所以走本方法的**内部扩展参数**。内存版不保留它们（端口上没有读路径暴露）。

        分配规则（ARCHITECTURE §10.4，两个后端必须逐字一致，否则分叉只在并发下显形）：

        * 号在**本方法内**按 ``MAX(generation) + 1`` 分配，``plan.generation`` 只作候选；
        * 候选号等于库里最新号、且冻结字节完全相同 → **重试**，不新增行，返回原号；
        * 其余情况（含候选号偏小 = 拿着过期 snapshot 算的）→ 分配新号。

        为什么"候选号偏小"不再报错：那正是两个驱动各自按旧 snapshot 算出同一个号的形状。
        报 `InvariantError` 会让一次并发被诊断成"我们算错了"，而**分配新号**让这条歧义
        从结构上消失 —— 号永远唯一，冲突没有机会发生。
        """
        self._require_write("put_plan")
        latest = max((p.generation for p in self._plan), default=0)
        if latest:
            head = next(p for p in self._plan if p.generation == latest)
            if (plan.generation == latest
                    and head.frozen_sha16 == plan.frozen_sha16
                    and head.frozen_text == plan.frozen_text):
                return latest                      # 重试：同一份冻结字节，号不变
        allocated = latest + 1
        self._plan = self._plan + (replace(plan, generation=allocated),)
        return allocated

    def put_system(self, prompt: SystemPrompt) -> None:
        self._require_write("put_system")
        self._system = prompt

    def append_event(self, event: "CompactionEvent | DegradationRecord") -> None:
        self._require_write("append_event")
        self._events.append(event)

    def put_measure(self, measure: "Measure") -> None:
        self._require_write("put_measure")
        for old in self._measures:
            if (old.session_id, old.turn_no, old.attempt) == (
                    measure.session_id, measure.turn_no, measure.attempt):
                # UNIQUE(session_id, turn_no, attempt)：重试不虚增样本。
                return
        self._measures.append(measure)

    # -- 提交 / 回滚 ------------------------------------------------------
    def commit(self) -> None:
        if self._finished:
            raise StoreError("事务已结束（commit/rollback 只能成功调用一次）")
        if not self._write:
            self._finished = True
            return
        failure = self._store.fail_commit_with
        if failure is not None:
            self._finished = True
            raise failure
        if self._body_key != self._store._state.body_key:
            self._rev += 1
        turns = tuple(sorted(self._turns.items()))
        self._store._state = _Committed(
            topics=tuple(self._topics),
            rev=self._rev,
            body_key=self._body_key,
            top_text=self._top_text,
            scope=self._scope,
            plans=tuple(self._plan),
            system=self._system,
            turns=turns,
            events=tuple(self._events),
            measures=tuple(self._measures),
        )
        self._store._absorb_allocator(self._alloc)
        self._finished = True

    def rollback(self) -> None:
        self._finished = True


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def iter_turn_indexes(store: StorePort) -> Iterator[int]:
    """按升序产出已存轮次号（给 Session 重建内存缓冲用）。"""
    snap = store.snapshot()
    if snap.turn_lo == 0 and snap.turn_hi == 0:
        return iter(())
    return iter(range(snap.turn_lo, snap.turn_hi + 1))


def turn_content_hash(text: str) -> str:
    """``turn.content_hash`` 的 64 位十六进制形态（SCHEMA 第 7 表）。"""
    return sha256_hex(text.encode("utf-8"))


def write_atom(atom_dir: str | None, data: bytes) -> str:
    """把 E 的原文副本按内容寻址写入 ``<store>.atoms/<sha16>``，返回该路径。

    同哈希只存一份（内容寻址）。没有 ``atom_dir`` 时**明确报错**而不是
    落到 cwd：猜一个目录会让同一份库在两台机器上指向不同文件。
    """
    if atom_dir is None:
        raise StoreError(
            "存储未提供 atom_dir：E 节点的内嵌副本无处可放。"
            "请构造存储时显式给出 atom_dir（SqliteStore 默认用 <库文件>.atoms）")
    os.makedirs(atom_dir, exist_ok=True)
    digest = sha256_hex(data)
    path = os.path.join(atom_dir, digest[:16])
    if os.path.exists(path):
        return path
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise StoreIOError(
            "写入原子副本失败：%s（%s）" % (path, exc.strerror or exc),
            errno=getattr(exc, "errno", None)) from exc
    return path


__all__ = [
    "MemoryStore",
    "StorePort",
    "StoreTxn",
    "iter_turn_indexes",
    "turn_content_hash",
    "write_atom",
]
