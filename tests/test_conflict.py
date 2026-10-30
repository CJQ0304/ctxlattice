# -*- coding: utf-8 -*-
"""F16（P9 被违反）：并发驱动的冲突**必须**与"库坏了"分开。

## 两半：**检出** 与 **反应**，各有各的用例

* **检出**（"确实发生了 P9 违反"）—— ``TestConcurrentDriverDetection``。它不注入
  异常，而是在 ``read_turns_with_hashes`` 上**改一份进来的正文**，模拟"库里那一轮
  不是我发的"。按 §10.6，F16 是不可逆动作（检出即永久停写），所以证据要求是
  「内容与缓冲不一致，且**连续两次观察**同一结论」—— 用例必须同时断言
  「第一次不动手」与「第二次动手」，少一半就测不住次数。
* **反应**（"既已判为并发冲突，会话必须怎么表现"）—— ``TestConflictStopsWritingForGood``。
  这一半仍然用注入的 ``SessionConflictError``（在 ``begin(write=True)`` 处抛）。

**为什么两半都要有**：只测反应时，"第一次观察就停写"与"永远不停写"两种实现都全绿 ——
它们都不违反处理器契约，只是把可修的抖动升级成不可逆停写，或者让检出形同虚设。
反过来只测检出时，停写覆盖面（三处写入口）没有用例守。

## 为什么这个反应值得单独钉

同一份 DDL 下有三句原本以为是"静默"的判断，拿 DDL 核完全错：

| 表 | 真实行为 | 后果 |
|---|---|---|
| `turn` | `turn_no` 由 `session.turn_seq` 串行发号，无重号无洞 | **P3 的连续性断言照样通过** —— 两段对话被交错进同一条链，检查抓不到 |
| `plans` | 两个驱动各自算出同一个 `generation` | 撞唯一键 —— 但按 P2 这只有在"号在事务外算"时才可能，所以那是指向**实现**的错，不指向调用方 |

也就是说误诊的代价是双向的：运维去查库，`integrity_check` 一定全绿；而真正的
处置（"这个 session 有两个驱动，停掉一个"）永远不会被执行。

## 最容易漏的一点：分派顺序

`SessionConflictError` 继承 `StoreError`。`classify()` 里任何 `isinstance(StoreError)`
排在它前面，都会把它吞成 `STORE_CORRUPT` —— **新类型白加，而且测试还会绿**：
`degraded` 仍然是 `True`，只有 `degrade_reason` 错了。而"降级为真、原因错"正是
唯一一种所有断言都通过、只有这个字段能拆穿的失败。

所以下面第一条断言不看"有没有降级"，只看**原因是不是它**。
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session            # noqa: E402
from ctxlattice.degrade import classify                        # noqa: E402
from ctxlattice.errors import SessionConflictError             # noqa: E402
from ctxlattice.policy import DegradeReason                    # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"


def _conflict() -> SessionConflictError:
    """驱动层真实的异常文本形状，原样照抄：映射就靠这个约束名。"""
    msg = "UNIQUE constraint failed: plans.session_id, plans.generation"
    try:
        return SessionConflictError(msg, op="commit/plan")
    except TypeError:
        return SessionConflictError(msg)


class _ConflictingStore(MemoryStore):
    """写事务抛 `SessionConflictError`，读一切正常。同时统计**写尝试次数**。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.conflict = False
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
            if self.conflict:
                raise _conflict()
        return super().begin(write=write)


class TestConflictIsNotCorruption(unittest.TestCase):
    def test_classify_dispatch_order(self):
        """★ 核心：`SessionConflictError` 必须归 `CONCURRENT_DRIVER`，不许被吞成 `STORE_CORRUPT`。"""
        code, _detail = classify(_conflict())
        self.assertIs(code, DegradeReason.CONCURRENT_DRIVER,
                      "被归类成 %r —— 说明 classify() 的分派顺序把子类吞了" % (code,))

    def test_conflict_is_a_store_error(self):
        """继承关系不能改：它**是**存储层故障，只是原因不同。

        改成不继承 `StoreError` 会让 F3 的"写失败 → 转只读"整条路径漏掉它。
        """
        self.assertTrue(issubclass(SessionConflictError, Exception))
        from ctxlattice.errors import StoreError

        self.assertTrue(issubclass(SessionConflictError, StoreError))

    def test_reason_is_distinguishable_from_corruption(self):
        """两种故障在 `degrade_reason` 上必须**可区分**。

        "降级为真、原因错"是所有断言都通过、只有这一个字段能拆穿的失败 ——
        也正因为如此，它最容易被"反正 `degraded` 是对的"放过。
        """
        from ctxlattice.errors import StoreCorruptError

        conflict = classify(_conflict())[0].value
        corrupt = classify(StoreCorruptError("头部计数不符"))[0].value
        self.assertNotEqual(conflict, corrupt)
        self.assertEqual(conflict, "concurrent_driver")
        self.assertEqual(corrupt, "store_corrupt")


class TestConflictStopsWritingForGood(unittest.TestCase):
    """判据是「**此后不再产生任何写事务**」，不是"不再报错"。"""

    def _session(self) -> tuple[Session, _ConflictingStore]:
        store = _ConflictingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=1, verbatim_keep=1),
                               system=SYSTEM)
        session.attribute(["T 冲突议题", "  P 子目的", "    R 一条要求"])
        session.send("正常第一轮")
        session.observe("收到")
        return session, store

    def test_send_survives_and_user_text_is_kept(self):
        session, store = self._session()
        store.conflict = True
        payload = session.send("冲突发生这一轮也得发出去")
        self.assertIn("冲突发生这一轮", payload.text, "用户本轮输入不可丢")
        self.assertTrue(payload.to_bytes())
        session.close()

    def test_next_turn_is_marked_concurrent_driver(self):
        """冲突之后的**每一轮**都必须标出来 —— 不可逆动作不许只标一次就沉底。

        ⚠️ 这一条走的是**压缩出口**（``compact_interval=1`` → 冲突落在
        ``_try_boundary`` 的写事务里），而压缩出口发生在装配**之前**
        （``_try_boundary`` 的返回值直接进 ``degraded``）。所以它**测不到**
        "装配之后还能不能补标"这件事。

        旧版本的 docstring 在这里写着「当轮 payload 已定稿、不改写；下一轮起必须标出来」。
        那句话是假的，而且它之所以能一直绿着，正是因为本条用例只走压缩出口 ——
        **同一个场景、两条出口，用例只碰到了本来就绿的那条**。
        装配之后的写盘出口由 ``test_conflict_turn_itself_is_marked`` 单独钉。
        """
        session, store = self._session()
        store.conflict = True
        session.send("冲突那一轮")
        payload = session.send("冲突之后的一轮")
        self.assertTrue(payload.degraded, "会话已不能写，必须让调用方知道")
        self.assertEqual(payload.degrade_reason,
                         DegradeReason.CONCURRENT_DRIVER.value)
        session.close()

    def test_conflict_turn_itself_is_marked(self):
        """★ 写盘期出口命中的**那一轮**就要标（§10.7）。

        为什么这条用例必须存在：另一条出口（压缩期）在装配**之前**就返回了降级标记，
        所以它那一轮本来就绿；而写盘出口在装配**之后**，只有实现肯事后 `replace`
        才可能绿。只测压缩出口，等于给"写盘期永不标"的实现发一张合格证。

        用例**自证前提**：断言命中的事件带 ``[src=write]``。没有这一句，一个退回
        压缩出口的实现（比如 `compact_interval` 调小）会让本用例照样绿，而它测的
        已经是另一条路了。

        场景：库为空 + 第 1 轮（F9 静默跳过压缩，见 ``_try_boundary``）→ 这一轮
        唯一的写事务就是 ``put_turn``，冲突只能落在这里。
        """
        store = _ConflictingStore()
        session = Session.open(store=store,
                              policy=Policy(compact_interval=1000, verbatim_keep=1),
                              system=SYSTEM)
        store.conflict = True
        payload = session.send("冲突发生在这一轮")

        fired = [r for r in session.degradation_log()
                 if r.reason is DegradeReason.CONCURRENT_DRIVER]
        self.assertEqual(len(fired), 1,
                         "写侧出口没触发（或触发了 %d 次）：本用例的前提不成立"
                         % len(fired))
        self.assertTrue(fired[0].detail.startswith("[src=write]"),
                        "命中的事件是 %r —— 这条用例要测的是**写盘**出口，"
                        "不是压缩出口" % (fired[0].detail[:30],))

        self.assertEqual((payload.degraded, payload.degrade_reason),
                         (True, DegradeReason.CONCURRENT_DRIVER.value),
                         "写盘期出口命中的当轮 payload 没标 / 原因错：调用方这一轮"
                         "拿到的是一份全绿的 payload，而库从这一轮起再没写进去过")
        self.assertIn("冲突发生在这一轮", payload.text, "本轮用户输入不可丢")
        self.assertFalse(session.stats().store_writable)
        session.close()

    def test_write_failure_marking_does_not_change_a_byte(self):
        """★ 事后 `replace` 只改元数据：两臂 `to_bytes()` 必须**逐字节相同**。

        这是这条改动唯一的风险点 —— 从这一版起，`degraded` / `degrade_reason`
        真的会在装配之后被改写。没有这条断言，将来有人把 `degrade_reason` 接进正文
        渲染时，前缀会在某一轮无故断裂，而原因看起来完全在别处。

        手法照 ``test_stability.py::test_over_budget_marking_does_not_change_a_byte``：
        两臂只差一件事（同一个 store 类型、同一份策略、同一句输入、同一个轮号），
        所以"正文该不该相同"没有歧义。
        """

        def run(conflict: bool):
            store = _ConflictingStore()
            session = Session.open(store=store,
                                   policy=Policy(compact_interval=1000, verbatim_keep=1),
                                   system=SYSTEM)
            store.conflict = conflict
            payload = session.send("同一句输入")
            session.close()
            return payload

        broken, healthy = run(True), run(False)
        self.assertTrue(broken.degraded, "写失败臂没标降级 —— 对照物不存在，下面的比较没有意义")
        self.assertFalse(healthy.degraded, "健康臂被标了降级 —— 两臂不再只差一件事")
        self.assertEqual(broken.to_bytes(), healthy.to_bytes(),
                         "标 degraded 改变了正文 —— 那前缀会在这一轮无故断裂")

    def test_no_write_transaction_after_conflict(self):
        """★ 停写：冲突之后 `begin(write=True)` 的次数必须**不再增长**。"""
        session, store = self._session()
        store.conflict = True
        session.send("触发冲突")
        after_first = store.write_attempts

        for i in range(3):
            session.send("冲突之后的第 %d 轮" % (i + 1))
            session.observe("收到")
        self.assertEqual(
            store.write_attempts, after_first,
            "冲突之后又发起了 %d 次写事务：判据不是「不再报错」，是「不再写」"
            % (store.write_attempts - after_first))

        # 归因也不许写
        self.assertIsNone(session.attribute(["T 新事物"]), "冲突之后 attribute() 不该写库")
        self.assertEqual(store.write_attempts, after_first)
        session.close()

    def test_store_rev_frozen_after_conflict(self):
        """可观测的第二个证据：库的 `rev` 不再变化（P4：内容指纹真变才 +1）。"""
        session, store = self._session()
        before = store.snapshot().rev
        store.conflict = True
        for i in range(3):
            session.send("第 %d 轮" % i)
            session.observe("收到")
        self.assertEqual(store.snapshot().rev, before,
                         "冲突之后库仍被改动 —— 说明还有一条写路径没停")
        session.close()


class TestTwoExitsReportSameReasonWithDifferentOrigins(unittest.TestCase):
    """同一个 ``reason``、两条出口 —— 只用 reason 分不出是哪一条。

    两条出口的**处置一样**（都彻底停写），但**要修的地方不一样**：

    * 写盘出口 = 同一 ``(turn_no, part)`` 换了字节 → 另一个驱动在写同一轮；
    * 压缩出口 = 压缩写事务里撞的 → 可能是 ``generation`` 算错（实现缺陷），
      也可能是对端恰好在压。

    报告里只有"F16 命中了"这一句话时，运维会去查进程模型；而若真相是号码算错，
    他查错了地方，且**每个断言都是绿的**。所以 ``detail`` 必须带机读前缀，
    「两条出口」这件事才不只是散文里的一个说法。

    前缀是给 grep、给报告解析器读的：中文散文改一次就断，而断言不会跟着红。
    """

    @staticmethod
    def _fired(session):
        return [r for r in session.degradation_log()
                if r.reason is DegradeReason.CONCURRENT_DRIVER]

    @staticmethod
    def _prefix(detail: str) -> str:
        head, sep, _rest = detail.partition("]")
        return head + sep if detail.startswith("[src=") else ""

    def _write_exit(self):
        """冲突落在**写盘**出口：库空 + 第 1 轮（F9 静默跳过压缩）→ 唯一写事务是 ``put_turn``。"""
        store = _ConflictingStore()
        session = Session.open(store=store,
                              policy=Policy(compact_interval=1000, verbatim_keep=1),
                              system=SYSTEM)
        store.conflict = True
        session.send("写盘出口")
        return session

    def _compaction_exit(self):
        """冲突落在**压缩**出口：第 1 轮冷启动压过一次，第 2 轮 interval 到期。"""
        store = _ConflictingStore()
        session = Session.open(store=store,
                              policy=Policy(compact_interval=1, verbatim_keep=1),
                              system=SYSTEM)
        session.attribute(["T 冲突议题", "  P 子目的", "    R 一条要求"])
        session.send("第 1 轮")
        session.observe("收到")
        store.conflict = True
        session.send("压缩出口")
        return session

    def test_two_exits_detail_prefixes_differ(self):
        write_side = self._write_exit()
        compaction_side = self._compaction_exit()
        try:
            w, c = self._fired(write_side), self._fired(compaction_side)
            self.assertEqual((len(w), len(c)), (1, 1),
                             "前提不成立：两条路径各应只记一条 concurrent_driver，"
                             "实测 %d / %d" % (len(w), len(c)))

            # 前提自证 ①：**同一个** reason —— 否则"靠前缀区分"这件事没有内容。
            self.assertEqual(w[0].reason, c[0].reason)

            # 前提自证 ②：两条前缀都必须存在。缺一个就退回"靠中文散文区分"。
            wp, cp = self._prefix(w[0].detail), self._prefix(c[0].detail)
            self.assertEqual(wp, "[src=write]",
                             "写盘出口没带机读前缀：%r" % (w[0].detail[:40],))
            self.assertEqual(cp, "[src=compaction]",
                             "压缩出口没带机读前缀：%r" % (c[0].detail[:40],))

            # ★ 判据本身。
            self.assertNotEqual(wp, cp,
                                "两条出口的前缀相同 —— 报告里仍然分不出是哪一条")
        finally:
            write_side.close()
            compaction_side.close()


class _CountingStore(MemoryStore):
    """只数写事务次数 —— 给"真的又写了一遍"这件事做**前提自证**。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)


class TestIndependentSessionsShareNothing(unittest.TestCase):
    """★ 误伤那一侧：两个互不相干的会话**不许**互相判成"另一个驱动"。

    F16 的处置不可逆（检出即永久停写），所以它需要**两半**用例：

    * 「该抓的抓到了」—— ``TestTwoRealDriversAgreeOnBothBackends``；
    * 「不该抓的没抓」—— 本类。

    只测前一半的后果：分叉检测的任何一处**比对范围被放宽**（窗口扫到整个库、
    store 被做成全局单例、指纹缓存被做成进程级、或者按 ``session_id`` 字符串而不是
    按 store 实例归属），两个不相干的会话都会互相判成对方是另一个驱动，然后
    **双方永久停写** —— 而所有现有用例仍然全绿，因为它们只断言"抓到了"。

    两个 ``Session`` 故意用**同一个 session_id 字符串**、**各自的 store 实例**：
    这正是"按字符串归属"和"按实例归属"会给出不同答案的那个配置。
    """

    SAME_ID = "同一个 session_id 字符串"

    def _pair(self):
        a_store = _CountingStore(store_id="box", session_id=self.SAME_ID)
        b_store = _CountingStore(store_id="box", session_id=self.SAME_ID)
        policy = Policy(compact_interval=3, verbatim_keep=1)
        a = Session.open(store=a_store, policy=policy, system=SYSTEM)
        b = Session.open(store=b_store, policy=policy, system=SYSTEM)
        a.attribute(["T 甲议题", "  P 甲目的", "    R 甲要求"])
        b.attribute(["T 乙议题", "  P 乙目的", "    R 乙要求"])
        return a, a_store, b, b_store

    def test_independent_stores_never_cross_talk(self):
        a, a_store, b, b_store = self._pair()
        for n in range(1, 9):
            for who, sess in (("A", a), ("B", b)):
                payload = sess.send("%s-%d" % (who, n))     # 两边**故意**不同
                self.assertFalse(payload.degraded,
                                 "%s 第 %d 轮被标了降级：%r（两个库互不相干，"
                                 "这一侧没有任何东西退化）"
                                 % (who, n, payload.degrade_reason))
                sess.observe("%s 的回复 %d" % (who, n))

        # ★ 前提自证：两边的第 1 轮文本**确实不同**、且确实在**各自的**库里的第 1 轮
        # （不是一个空真：若两边文本相同，"跨 store 比对"放宽了也撞不上，本用例就什么都没测）。
        a_first = a_store.read_turns(1, 1)[0].user_text
        b_first = b_store.read_turns(1, 1)[0].user_text
        self.assertEqual((a_first, b_first), ("A-1", "B-1"),
                         "前提不成立：两边第 1 轮不是 A/B 各自的内容（实测 %r / %r）"
                         % (a_first, b_first))
        self.assertIsNot(a_store, b_store, "前提不成立：两个 Session 拿到的是同一个 store")

        # **两边都查**。只查一边，"第一边停写、第二边没事"这种单向污染照样绿。
        for who, sess, store in (("A", a, a_store), ("B", b, b_store)):
            fired = [r.detail for r in sess.degradation_log()
                     if r.reason is DegradeReason.CONCURRENT_DRIVER]
            self.assertEqual(
                fired, [],
                "%s 侧出现了 concurrent_driver：另一个 store 上的会话被当成了"
                "「同一个 session 的第二个驱动」→ 这一侧永久停写，而它什么都没做错。"
                "事件：%r" % (who, fired))
            self.assertFalse(sess._writes_halted, "%s 侧被误判成 P9 违反，停写了" % who)
            self.assertTrue(sess.stats().store_writable,
                            "%s 侧 store_writable=False：不可逆动作被误伤触发" % who)
            # ★「没停写」必须由「真的还在写」来证明：一个什么都不写的实现也满足上面三条。
            self.assertEqual(store.snapshot().turn_hi, 8,
                             "%s 侧库里有 %d 轮而不是 8 轮 —— 它早就没在写了"
                             % (who, store.snapshot().turn_hi))

        # 停下来之后再发一轮也必须成功（判据是"此后仍能写"，不是"当时没报错"）。
        self.assertTrue(a.send("A-9").to_bytes())
        self.assertTrue(b.send("B-9").to_bytes())
        a.close()
        b.close()

    def test_replaying_identical_bytes_is_idempotent(self):
        """★ 「凡重写必抛」的实现能让**所有**分叉用例全绿，而它会把每次重试都变成停写。

        会话层的重放路径：同一句 ``user_text`` 在**同一轮号**下再落一遍
        （回执丢了、调用方重试的形状）。
        判据是"静默通过 + 不落到任何状态位"，而前提是"**真的又发起了一次写事务**" ——
        少了这条前提自证，一个"第二次根本没写"的实现也满足全部断言，
        于是这条用例会变成在测"什么都没发生"。
        """
        store = _CountingStore(store_id="box", session_id="idem")
        session = Session.open(store=store,
                               policy=Policy(compact_interval=1000, verbatim_keep=1),
                               system=SYSTEM)
        session.send("同一份字节")
        session.observe("收到")
        turn = session._turn_log[-1]

        before = store.write_attempts
        session._persist_turn(turn)                 # 同号同字节，再来一遍
        self.assertEqual(store.write_attempts, before + 1,
                         "第二次落盘根本没发起写事务 —— 下面的断言测不到幂等")
        self.assertFalse(session._writes_halted,
                         "同号同字节被判成了分叉：每次重试都会永久停写")
        self.assertTrue(session.stats().store_writable)
        self.assertEqual([r.detail for r in session.degradation_log()
                          if r.reason is DegradeReason.CONCURRENT_DRIVER], [])
        self.assertEqual(store.snapshot().turn_hi, 1,
                         "重复落盘把同一轮记成了两轮 —— 幂等重放不该追加")
        session.close()


class _TamperingStore(MemoryStore):
    """在**第二次观察第 2 轮**时，回一份被改过的 ``user_text``。

    为什么不注入异常：注入测的是"我写的那行 raise"，而这里要验的是
    **判定逻辑本身**（比对 + 二次确认）。改数据比改控制流更接近真实的 P9 违反 ——
    真实世界里另一个驱动留下的就是"同一轮、不同内容"。

    触发条件写成"任何覆盖第 2 轮的读区间，第 2 次起改数据"，而不是写死
    ``(2, 2)``：实现完全可能一次读一段窗口（``(1,2)`` / ``(2,3)``），
    按区间写死会让用例与实现细节耦合，改窗口就红。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.observations_of_turn2 = 0
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)

    def read_turns_with_hashes(self, lo: int, hi: int):
        turns, hashes = super().read_turns_with_hashes(lo, hi)
        if not (lo <= 2 <= hi):
            return turns, hashes
        self.observations_of_turn2 += 1
        if self.observations_of_turn2 < 2:
            return turns, hashes
        return (tuple(
            replace(t, user_text="另一个驱动写的第 2 轮") if t.index == 2 else t
            for t in turns
        ), hashes)


class TestConcurrentDriverDetection(unittest.TestCase):
    """P9 检出：**一条**通道（内容与缓冲不一致）+ **两次**观察。"""

    def _session(self) -> tuple[Session, _TamperingStore]:
        store = _TamperingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=50, verbatim_keep=2),
                               system=SYSTEM)
        session.attribute(["T 冲突议题", "  P 子目的", "    R 一条要求"])
        session.send("第 1 轮")
        session.observe("收到")
        session.send("第 2 轮")
        session.observe("收到")
        return session, store

    def test_first_observation_does_not_halt(self):
        """★ 第一次观察**不许**动手 —— 否则可修的抖动被升级成不可逆停写。

        时序（这就是这条用例真正在钉的东西）：第 2 轮进缓冲时，探测发生的时刻
        **本轮还没进缓冲** —— 所以"观察第 2 轮"最早发生在第 3 轮。用例里写死
        "第几次 send 对应第几次观察"，正是为了让"次数"这件事有断言可依。
        """
        session, store = self._session()
        payload = session.send("第 3 轮")        # 第 1 次观察第 2 轮
        self.assertGreaterEqual(store.observations_of_turn2, 1,
                                "用例没观察到第 2 轮，下面的断言没有意义")
        self.assertFalse(session._writes_halted,
                         "第一次不一致就停写了（§10.6：不可逆动作要两次观察）")
        self.assertNotEqual(payload.degrade_reason,
                            DegradeReason.CONCURRENT_DRIVER.value)
        session.close()

    def test_second_observation_halts_for_good(self):
        """★ 第二次观察同一结论 → 停写 + 原因 + ``store_writable`` 三者齐。"""
        session, store = self._session()
        session.send("第 3 轮")          # 第 1 次观察：只登记怀疑
        self.assertFalse(session._writes_halted)
        payload = session.send("第 4 轮")  # 第 2 次观察：同一结论 → 停写
        self.assertTrue(session._writes_halted, "第二次观察同一结论仍未停写")
        # §10.7：断言**二元组**。只断言 degraded=True 会放过"降级为真、原因被换掉"。
        self.assertEqual((payload.degraded, payload.degrade_reason),
                         (True, DegradeReason.CONCURRENT_DRIVER.value))
        self.assertFalse(session.stats().store_writable)

        after_halt = store.write_attempts
        for i in range(3):
            session.send("停写之后第 %d 轮" % (i + 1))
            session.observe("收到")
        self.assertEqual(store.write_attempts, after_halt,
                         "停写之后又发起了 %d 次写事务"
                         % (store.write_attempts - after_halt))
        session.close()

    def test_clean_buffer_never_halts(self):
        """负控：缓冲与库**始终一致**时，一条降级都不许由检测产生。

        没有这一条，一个"永远停写"的实现也能让上面两条全绿。
        用**干净**的 ``MemoryStore``（不是 ``_TamperingStore``）：这一条要证明的是
        "没有外来证据就不动手"，掺进篡改器就变成在测篡改器。
        """
        store = MemoryStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=50, verbatim_keep=2),
                               system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        for i in range(1, 6):
            payload = session.send("正常第 %d 轮" % i)
            self.assertFalse(payload.degraded, "正常轮被标降级了")
            session.observe("收到")
        self.assertFalse(session._writes_halted)
        session.close()


class TestReasonPriorityIsByReversibility(unittest.TestCase):
    """一条原因会盖住另一条 —— 所以必须规定**谁盖谁**，并双向验证。

    判据是「这条原因下一轮还会不会成立」，不是「它是被谁记下来的」。
    ``missing_assistant`` 下一轮就没了，``concurrent_driver`` 永远在；前者盖住后者
    等于最该修的那条从报告里消失，而所有断言仍然是绿的。
    """

    def test_irreversible_wins_when_recorded_first(self):
        from ctxlattice.policy import pick_reason

        self.assertEqual(
            pick_reason(DegradeReason.CONCURRENT_DRIVER.value,
                        DegradeReason.MISSING_ASSISTANT.value),
            DegradeReason.CONCURRENT_DRIVER.value)

    def test_irreversible_wins_when_recorded_second(self):
        from ctxlattice.policy import pick_reason

        self.assertEqual(
            pick_reason(DegradeReason.MISSING_ASSISTANT.value,
                        DegradeReason.CONCURRENT_DRIVER.value),
            DegradeReason.CONCURRENT_DRIVER.value)

    def test_same_class_keeps_the_first(self):
        from ctxlattice.policy import pick_reason

        self.assertEqual(
            pick_reason(DegradeReason.MISSING_ASSISTANT.value,
                        DegradeReason.ENCODING_ERROR.value),
            DegradeReason.MISSING_ASSISTANT.value,
            "同层必须保留第一个：否则同一个会话的 degrade_reason 会随轮次抖动，"
            "报告里读不出稳定结论")

    def test_end_to_end_degrade_reason_is_not_overwritten(self):
        """端到端：先发生 ``missing_assistant``，再检出 ``concurrent_driver``。

        payload 必须报后者 —— 前者是一次性的，后者是永久的。
        """
        store = _TamperingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=50, verbatim_keep=2),
                               system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        session.send("第 1 轮")          # 不 observe：制造 missing_assistant
        self.assertFalse(session._writes_halted)
        session.send("第 2 轮")
        self.assertFalse(session._writes_halted)
        session.send("第 3 轮")          # 第 1 次观察：只登记怀疑
        self.assertFalse(session._writes_halted)
        payload = session.send("第 4 轮")   # 第 2 次观察 → 检出
        self.assertEqual(payload.degrade_reason,
                         DegradeReason.CONCURRENT_DRIVER.value,
                         "永久的 concurrent_driver 被一次性的 missing_assistant 盖住了")
        session.close()

class _LateTamperingStore(MemoryStore):
    """第 2 轮**在库里**被改掉，而 ``hi`` 是第 3 轮 —— 不一致的那一轮**不是** ``hi``。

    这正是真 bug 的形状：两个驱动交替写时，我这边能观察到的错位通常落在
    **更早**的那一轮上，而 ``hi`` 指向的是对方刚写进去的那一轮。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tamper = False
        self.observations_of_turn2 = 0

    def read_turns_with_hashes(self, lo: int, hi: int):
        turns, hashes = super().read_turns_with_hashes(lo, hi)
        if not self.tamper or not (lo <= 2 <= hi):
            return turns, hashes
        self.observations_of_turn2 += 1
        return (tuple(
            replace(t, user_text="另一个驱动写的第 2 轮") if t.index == 2 else t
            for t in turns
        ), hashes)


class TestWindowMustReachBack(unittest.TestCase):
    """窗口必须**回看得到**不一致的那一轮，否则检出不成立。

    这条用例存在的理由是一条变异检验的失败：把 ``FOREIGN_PROBE_WINDOW`` 改成 1，
    原来那三条检出用例**一条都不红** —— 因为它们篡改的那一轮恰好就是 ``hi``，
    窗口 = 1 也够。于是"窗口回看"这件事没有任何用例守着，而它正是修掉的那个
    bug 的形状（真双驱动 0 次命中，原因就是不一致的那一轮不在 ``hi`` 上）。

    断言落在**行为**上，不落在常量上：

    * 窗口 = 1 → 只看 ``hi``（第 3 轮，与缓冲一致）→ 永远看不到第 2 轮 → 本用例红；
    * 窗口 ≥ 2 → 区间覆盖第 2 轮 → 本用例绿。

    断言常量 ``FOREIGN_PROBE_WINDOW >= 2`` 看起来更直接，但那是把实现细节抄进测试：
    它既不证明任何行为，还会在有人把窗口调成 5 时无谓地红。
    """

    def test_mismatch_on_an_earlier_turn_is_still_detected(self):
        store = _LateTamperingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=50, verbatim_keep=2),
                               system=SYSTEM)
        session.attribute(["T 冲突议题", "  P 子目的", "    R 一条要求"])
        for i in range(1, 4):                 # 1..3 全是我写的，库与缓冲一致
            session.send("第 %d 轮" % i)
            session.observe("收到")

        # 前提自证：不一致的是第 2 轮，而 hi 是第 3 轮。
        self.assertEqual(store.snapshot().turn_hi, 3,
                         "场景前提不成立：hi 不再是第 3 轮，这条用例测的东西变了")
        store.tamper = True

        session.send("第 4 轮")               # 第 1 次观察
        self.assertGreaterEqual(
            store.observations_of_turn2, 1,
            "探测窗口没有回看到第 2 轮（不一致的那一轮不是 hi，窗口太窄就永远看不见）")
        self.assertFalse(session._writes_halted, "第一次观察就停写了")

        payload = session.send("第 5 轮")      # 第 2 次观察 → 成立
        self.assertTrue(session._writes_halted,
                        "同一处不一致观察了两次仍未停写")
        self.assertEqual((payload.degraded, payload.degrade_reason),
                         (True, DegradeReason.CONCURRENT_DRIVER.value))
        session.close()
    def test_end_to_end_irreversible_not_overwritten_by_later_event(self):
        """反方向：``concurrent_driver`` 先成立，之后来一条 ``missing_assistant``。

        与非对称的另一半配对。**只测一个方向等于没测**：实现若写成"后写覆盖先写"，
        两个方向的用例里总有一个会绿，于是改坏了也发现不了。
        """
        store = _TamperingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=50, verbatim_keep=2),
                               system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        for i in range(1, 5):          # 走到第二次观察 → 停写
            session.send("第 %d 轮" % i)
            session.observe("收到")
        self.assertTrue(session._writes_halted, "前提不成立：没有检出 concurrent_driver")

        session.send("没有 observe 的一轮")      # 下一轮起 missing_assistant 成立
        payload = session.send("再下一轮")
        self.assertEqual(payload.degrade_reason,
                         DegradeReason.CONCURRENT_DRIVER.value,
                         "不可逆原因被之后发生的可逆原因改写了")
        session.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
