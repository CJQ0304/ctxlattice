# -*- coding: utf-8 -*-
"""端口契约：``put_plan`` 的**返回号**。

## 为什么这个返回值值得两条用例

``generation`` 在写事务内按 ``MAX(generation) + 1`` 分配（ARCHITECTURE §10.4），
``plan.generation`` 进到 ``put_plan`` 时只是**候选号**。于是有三件事必须同时成立：

1. **实现返回它分配到的号** —— 类型注解必须是 ``int``；
2. **调用方采纳返回号** —— 否则下一次压缩拿着旧号再算一遍，候选号与库里的号重新错开；
3. 同号同字节 → **不新增行**（重试），同号不同字节 → **发新号**（两个驱动拿旧快照）。

第 3 条以前被写成「同号不同字节 → ``InvariantError``」，那条分支现在是**不可达**的
（号在事务内分配，"撞号"这个前提消失了），所以它已经被删掉，而不是留着当防御。

## 注解那条用例在挡什么（顾图南发现的真残留）

``_SqliteTxn.put_plan`` 的实现从 A 裁决起就有 ``return latest`` / ``return allocated``，
而注解一直写着 ``-> None``。**注解与实现不一致，注解是错的**，而它错的方式很贵：

下一个读者按注解读代码，会把两个 ``return`` 当成死代码删掉（"反正返回 None"）。
删完不会有任何测试变红 —— 压缩会永远沿用候选号，两个驱动重新错开，而
"号错"这件事只在并发下显形，且没有任何断言盯着它。

所以注解本身要有一条用例，且要**三处同时**（协议 / 内存版 / SQLite 版）——
只钉一处，另外两处漂走时仍然全绿。
"""

from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session, SqliteStore   # noqa: E402
from ctxlattice.store import StoreTxn, _MemoryTxn                  # noqa: E402
from ctxlattice.store_sqlite import _SqliteTxn                     # noqa: E402
from ctxlattice.text import sha16_text                             # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"


class TestPutPlanAnnotation(unittest.TestCase):
    """三处声明必须同时是 ``int``。"""

    def test_all_three_declarations_say_int(self):
        # 自证有效性：先确认这三个对象真的存在、真的有这个方法。否则下面的
        # ``getattr`` 抛 AttributeError 会被误读成"注解不对"，而实际是对象换名了。
        declarations = {
            "StoreTxn（协议）": StoreTxn,
            "_MemoryTxn": _MemoryTxn,
            "_SqliteTxn": _SqliteTxn,
        }
        for label, owner in declarations.items():
            with self.subTest(declaration=label):
                self.assertTrue(hasattr(owner, "put_plan"),
                                "%s 上没有 put_plan：下面验的不是它" % label)
                ann = inspect.signature(owner.put_plan).return_annotation
                self.assertIn(
                    ann, (int, "int"),
                    "%s.put_plan 的返回注解是 %r。它**返回实际分配到的 generation**，"
                    "注解必须写 int —— 写成 None 会让下一个读者把两个 return 当死代码"
                    "删掉，而那时没有任何用例会红" % (label, ann))


class TestPutPlanReturnValueContract(unittest.TestCase):
    """行为级：同号同字节返原号 / 同号不同字节返更大号 / 两次都不抛。两后端各跑一遍。"""

    def _real_plan(self, store) -> object:
        """跑一次真压缩，拿一份**合法**且已经是库里最新一代的 plan。"""
        session = Session.open(store=store,
                               policy=Policy(compact_interval=1, verbatim_keep=1),
                               system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        session.send("第一轮")
        session.observe("收到")
        session.send("第二轮")
        plan = store.snapshot().plan
        self.assertIsNotNone(plan, "这一段没压出 plan，下面的用例测不到东西")
        return plan

    def _contract(self, store, count_plans) -> None:
        plan = self._real_plan(store)
        before = count_plans()

        # ① 候选号 == 最新号，且冻结字节逐字相同 → 重试，不新增行，返回原号。
        with store.begin(write=True) as txn:
            retried = txn.put_plan(plan)
            txn.commit()
        self.assertEqual(retried, plan.generation,
                         "同一份冻结字节被当成了新的一代（返回 %r，候选 %r）"
                         % (retried, plan.generation))
        self.assertEqual(count_plans(), before, "重试提交新增了 plans 行")

        # ② 候选号 == 最新号，但字节不同（两个驱动拿旧 snapshot 的形状）→ 发新号。
        text = plan.frozen_text + "\n[另一份字节]"
        other = replace(plan, frozen_text=text, frozen_sha16=sha16_text(text))
        with store.begin(write=True) as txn:
            allocated = txn.put_plan(other)      # 不抛：判据是字节，不是号
            txn.commit()
        self.assertGreater(allocated, plan.generation,
                           "过期候选号没有被重新分配（返回 %r，最新号 %r）"
                           % (allocated, plan.generation))
        self.assertEqual(count_plans(), before + 1)
        self.assertEqual(store.snapshot().plan.generation, allocated,
                         "分配到的号没有成为库里最新的一代")

    def test_memory_backend(self):
        store = MemoryStore(store_id="m", session_id="s")
        self._contract(store, lambda: len(store._state.plans))

    def test_sqlite_backend(self):
        tmp = tempfile.mkdtemp()
        try:
            store = SqliteStore(os.path.join(tmp, "store.db"), store_id="t",
                                session_id="s1", transcript_hash="b" * 64)
            self._contract(store, lambda: self._sqlite_plan_count(store))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _sqlite_plan_count(store) -> int:
        import sqlite3
        conn = sqlite3.connect(store.path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
