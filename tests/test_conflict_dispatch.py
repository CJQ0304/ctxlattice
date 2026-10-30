# -*- coding: utf-8 -*-
"""F16 的**发射边界**：什么才算"两个驱动"，什么只是约束违反。

## 这一条守的是一个不可逆误判

F16（P9 被违反）的处置是**本 Session 此后彻底停写** —— 它不可逆，且没有任何信号
说明它被误判了。所以它的触发条件必须比可逆降级更硬：

* 可逆（F1 / F3 / F11）：判错了，下一轮还能自己恢复；
* **不可逆（F16）**：判错了，这个进程从此不再写库，而它看起来完全正常。

原实现里除了那个精确的 SELECT 预检，还额外挂了一个
`except StoreCorruptError → raise SessionConflictError`。方向错了：

1. `_all` 会把本事务内**任何**约束违反（CHECK / FK / schema 不符）都映射成
   `StoreCorruptError`。一次 `CHECK (r_first_turn <= epoch_turn + 1)` 违反就会被
   永久说成"两个驱动在抢"；
2. 它根本捕获不到它想捕获的那个冲突：本事务由 `BEGIN IMMEDIATE` 开启，写锁
   从 BEGIN 持有到 COMMIT，SELECT 预检与 INSERT 处在**同一个排他区间**内，
   另一驱动无法在中间插入。

所以那个 except 的命中率是 0，而误报的代价是永久停写。已删除。

## 为什么用真 SQLite 而不是 fake

fake store 的"约束违反"是我自己 `raise` 的，测的是我写的那行 `raise`；
真库才会给出 `sqlite3.IntegrityError("CHECK constraint failed: ...")`——
而**它被映射成哪个类型**正是这条用例要验的东西。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import Policy, Session, SqliteStore              # noqa: E402
from ctxlattice.degrade import classify                          # noqa: E402
from ctxlattice.errors import InvariantError, SessionConflictError  # noqa: E402
from ctxlattice.policy import DegradeReason                      # noqa: E402
from ctxlattice.text import sha16_text                           # noqa: E402


class TestConstraintViolationIsNotConflict(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "store.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self) -> SqliteStore:
        return SqliteStore(self.path, store_id="t", session_id="s1",
                           transcript_hash="b" * 64)

    def _real_plan(self):
        """跑一次真压缩，拿一份**合法**的 plan（用它派生违规值）。"""
        store = self._store()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=1, verbatim_keep=1),
                               system="你是一个严谨的助手。\n")
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        session.send("第一轮")
        session.observe("收到")
        session.send("第二轮")
        plan = store.snapshot().plan
        session.close()
        self.assertIsNotNone(plan, "这一段没产生 plan，下面的用例测不到东西")
        return plan

    def test_check_violation_is_an_invariant_not_conflict(self):
        """★ `CHECK (r_first_turn <= epoch_turn + 1)` 违反 → `InvariantError` → INTERNAL。

        ★★ 这一条改过两次，第二次是本轮 A 裁决的直接后果：

        1. 原先要求 ``StoreCorruptError``（F1）。改的理由是**处置链**：F1 的处置是
           "库读不出来，此后只用缓冲" —— 可库明明是好的，压缩器只是算错了一个字段。
           ``InvariantError`` 的处置是**只停压缩**（``_compaction_blocked``），
           读写都不动。
        2. 触发字段从 ``generation = 0`` 换成 ``r_first_turn = epoch_turn + 5``：
           ``generation`` 现在由 ``put_plan`` 在**写事务内**分配，调用方给的只是**候选号**，
           校验它等于校验一个不参与写库的字段（假闸门）。而 ``r_first_turn`` 是压缩器
           真算出来的，仍然是"压缩算错了"的典型形状。

        不变的是那条更重要的半边：**不许译成 F16**（不可逆停写 + 指向调用方）。
        """
        plan = self._real_plan()
        bad = replace(plan, r_first_turn=plan.epoch_turn + 5)   # R 段不能从边界之后起步

        store = self._store()
        store.open()
        txn = store.begin(write=True)
        try:
            with self.assertRaises(InvariantError) as cm:
                txn.put_plan(bad)
        finally:
            try:
                txn.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败不该盖住真正的断言
                pass
            store.close()

        self.assertNotIsInstance(
            cm.exception, SessionConflictError,
            "CHECK 违反被说成了'两个驱动在抢' —— 这会让 F16 永久停写，"
            "而它其实只是本进程算错了一个字段")
        code, _detail = classify(cm.exception)
        self.assertIs(code, DegradeReason.INTERNAL,
                      "被归类成 %r：这条错的性质是**实现缺陷**（压缩器算错了），"
                      "既不是库损坏（诊断会引向 integrity_check，必然全绿），"
                      "也不是并发" % (code,))

    def test_stale_generation_is_reallocated_not_rejected(self):
        """★ 候选号过期（同号 + 不同字节）→ **重新分配**，既不抛错也不是"两个驱动"。

        ★★★ 这一条的期望**整条换掉了**（A 裁决）：以前它断言抛 ``InvariantError``，
        依据是"号必须在写事务内分配，所以同号不同字节只可能是实现 bug"—— 而那个依据
        在当时**不成立**（``compaction.py`` 是压缩前按 snapshot+1 算的）。

        现在号真的在写事务内分配了，于是"同号"这件事不再有诊断价值：两个驱动各自拿
        旧 snapshot 都会算出同一个候选，正确反应是**给后来者一个新号**，而不是报错。
        报错会让一次并发被诊断成"我们算错了"，而且（旧版）会让会话永久停写。

        判据三条：不抛异常 / 返回号 = 最新号 + 1 / 库里 generations 严格递增无重复。
        """
        plan = self._real_plan()
        # 另一份**自洽**的冻结字节（sha16 必须与正文对得上，否则 snapshot() 的
        # 自洽校验会先报"冻结块被改坏"，把这条用例测的东西盖掉）。
        text = plan.frozen_text + "\n[另一份字节]"
        other = replace(plan, frozen_text=text, frozen_sha16=sha16_text(text))

        store = self._store()
        store.open()
        txn = store.begin(write=True)
        try:
            allocated = txn.put_plan(other)      # 候选号 == 库里最新号，但字节不同
            txn.commit()
        except BaseException:
            txn.rollback()
            raise
        finally:
            store.close()

        self.assertGreater(allocated, plan.generation,
                           "过期候选号没有被重新分配（返回 %r，最新号 %r）"
                           % (allocated, plan.generation))
        snapshot = store.snapshot()
        self.assertEqual(snapshot.plan.generation, allocated,
                         "分配到的号没有成为库里最新的一代")


class TestGenerationIsAllocatedInsideTheTransaction(unittest.TestCase):
    """A 裁决的验收判据：**两个真驱动**各自压缩，不出现 `InvariantError`。

    判据只有两条，但足以区分"号在事务内分配"与"号在外面算好再塞进来"：

    * 全程没有一条 `internal` 降级（`InvariantError` 的唯一出口）；
    * 库里 `plans.generation` **严格递增、无重复** —— 后来者拿到的是更大的号，
      而不是撞上同一个号再靠翻译成某个异常来收场。

    为什么不用 fake：`BEGIN IMMEDIATE` 拿锁的顺序、事务可见性都是真库行为；
    fake 里的"并发"是我自己写的那几行 raise。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "two-drivers.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _generations(self) -> list[int]:
        import sqlite3

        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT generation FROM plans WHERE session_id = ? ORDER BY generation",
                ("s1",)).fetchall()
        finally:
            conn.close()
        return [int(r[0]) for r in rows]

    def test_two_drivers_compacting_never_collide(self):
        """B 侧永远拿着"别人提交之前"的号去压缩 —— 这正是候选号过期的形状。

        只用两个裸 ``SqliteStore`` 是**测不出**这件事的：单线程交替时，每一轮
        ``snapshot()`` 都读到最新号，候选号永远不过期，于是"号在事务内分配"与
        "号在外面算"表现完全一样（我第一版就是这么写的，变异检验没咬住）。
        所以这里给 B 套一个"plan 号永远报小 1"的端口 —— 把并发窗口里的那个过期快照
        **做成确定性的**。

        变异判据（实测）：把 ``put_plan`` 的分配改回 ``plan.generation``，这条用例红 ——
        撞 UNIQUE → ``StoreCorruptError`` → F1 → 会话转 buffer_only。
        """

        class _OneBehindStore(SqliteStore):
            """快照里**看不到 plan** —— 等价于"我在别人建出第一代 plan 之前读的快照"。

            为什么不用"号报小 1"：那样 B 的候选号恰好等于 A 已提交的号，而冻结字节
            也相同（两边发同样的文本），于是走的是**重试**分支（同号 + 同字节 → 不新增行），
            唯一键根本不会撞 —— 变异检验因此咬不住。看不到 plan 才是真正的"过期快照"：
            候选号从 1 起算，而库里已经有 1。
            """

            def snapshot(self):
                return replace(super().snapshot(), plan=None)

        def driver(store_factory) -> Session:
            session = Session.open(
                store=store_factory(),
                policy=Policy(compact_interval=2, verbatim_keep=1),
                system="你是一个严谨的助手。\n")
            session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
            return session

        def plain() -> SqliteStore:
            return SqliteStore(self.path, store_id="t", session_id="s1",
                               transcript_hash="c" * 64)

        def one_behind() -> SqliteStore:
            return _OneBehindStore(self.path, store_id="t", session_id="s1",
                                   transcript_hash="c" * 64)

        a, b = driver(plain), driver(one_behind)
        # 两边发**同样的文本**：这样 turn 内容不会分叉，F16 的内容通道不会介入，
        # 于是下面任何一条 store_corrupt / internal 都只能来自 plans 的号。
        # （发不同文本时，F16 会先停写，把这条用例要测的东西盖掉 —— 我试过。）
        for i in range(1, 8):                 # 交替写：B 一直在用过期候选号
            text = "第 %d 轮" % i
            a.send(text)
            b.send(text)
            a.observe("收到")
            b.observe("收到")

        for name, session in (("A", a), ("B", b)):
            reasons = [(rec.reason.value if hasattr(rec.reason, "value") else rec.reason)
                       for rec in session.degradation_log()]
            self.assertNotIn(
                "internal", reasons,
                "%s 侧出现了 internal（= InvariantError，号还是在外面的算法）" % name)
            self.assertNotIn(
                "store_corrupt", reasons,
                "%s 侧出现了 store_corrupt：过期候选号撞了唯一键，"
                "说明号没在写事务内分配" % name)
        self.assertTrue(b.stats().store_writable,
                        "B 侧被降成了只读：过期候选号撞了唯一键（号没在事务内分配）")

        gens = self._generations()
        self.assertGreaterEqual(len(gens), 2,
                                "这段没有产生两代 plan，用例前提不成立：%r" % (gens,))
        self.assertEqual(gens, sorted(set(gens)),
                         "generations 不是严格递增无重复：%r" % (gens,))
        a.close()
        b.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
