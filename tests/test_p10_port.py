# -*- coding: utf-8 -*-
"""P10 的**端口形状**：一对证据要么同一次交出，要么在结构上拿不到。

    tests/test_p10_port.py

这四条原先是 `scratch/probe_p10_port_shape.py` —— 约定五的**临时落点**。
它被 `ARCHITECTURE.md` §10.6 引用为「P10 已核对完毕（实测，非阅读）」的唯一证据，
所以 `scratch/` 一删，那条结论就只剩一句无人复核的话。升格成用例是约定五给的
两个合法选项里唯一不丢信息的那一个。

## 每一格挡什么

| # | 断言 | 挡的实现 |
|---|---|---|
| ① | 组合入口只开 **1** 个事务（1 BEGIN / 1 COMMIT） | 拆成两次读、各自开事务 —— "同快照"就成了运气 |
| ② | 指纹与**同次**读回的正文逐条自洽 | 指纹来自另一次读（两次之间插进一次外部提交就错版） |
| ③ | **旧形状确实会错版**（外部提交 → 正文 v1 + 指纹 v2） | 前两格在"两次调用但恰好没撞上"的实现上会绿 —— 这一格证明缝真的存在 |
| ④ | 端口上**没有**"只读指纹"的入口 | 误用面靠禁令而不是靠"拿不到那个输入" |

第 ③ 格不是演示、不是讲故事：**没有它，①② 只能证明"我没看见缝"**，而"没看见"
在 §二之二 那张表里是最贵的一种阴性结果。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice.model import Message, Role, Turn          # noqa: E402
from ctxlattice.store_sqlite import SqliteStore            # noqa: E402

SESSION = "s-case8"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestBodyAndHashComeFromOneSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ctxlattice-p10-")
        self.path = os.path.join(self.tmp, "s.db")
        store = SqliteStore(self.path, session_id=SESSION)
        store.open()
        try:
            with store.begin(write=True) as txn:
                for n in (1, 2, 3):
                    txn.put_turn(Turn(
                        index=n,
                        user_text="第 %d 轮提问" % n,
                        replies=(Message(role=Role.ASSISTANT, text="第 %d 轮回答" % n),),
                    ))
                txn.commit()
        finally:
            store.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self) -> SqliteStore:
        return SqliteStore(self.path, session_id=SESSION)

    def test_composite_entry_opens_exactly_one_transaction(self):
        """① 一次 BEGIN / 一次 COMMIT —— 否则"同快照"退化成运气。"""
        store = self._open()
        store.open()
        try:
            counter = {"begin": 0, "commit": 0}
            real_exec = store._exec

            def counting_exec(conn, sql, *args, **kwargs):
                head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
                if head == "BEGIN":
                    counter["begin"] += 1
                elif head == "COMMIT":
                    counter["commit"] += 1
                return real_exec(conn, sql, *args, **kwargs)

            store._exec = counting_exec                     # type: ignore[method-assign]
            try:
                store.read_turns_with_hashes(1, 3)
            finally:
                store._exec = real_exec                     # type: ignore[method-assign]
        finally:
            store.close()

        self.assertEqual(counter["begin"], 1,
                         "组合入口开了 %d 个事务：正文与指纹分处不同快照，P10 不成立"
                         % counter["begin"])
        self.assertEqual(counter["commit"], 1,
                         "组合入口提交了 %d 次（开了 %d 次）"
                         % (counter["commit"], counter["begin"]))

    def test_hashes_are_consistent_with_the_bodies_of_the_same_read(self):
        """② 指纹与**同次**读回的正文逐条自洽。"""
        store = self._open()
        store.open()
        try:
            turns, hashes = store.read_turns_with_hashes(1, 3)
        finally:
            store.close()

        self.assertEqual(len(turns), 3, "读回 %d 轮，应为 3 轮" % len(turns))
        expected = 0
        for turn in turns:
            for part, msg in enumerate(turn.messages()):
                key = (turn.index, part)
                self.assertIn(key, hashes, "缺少指纹 %r" % (key,))
                self.assertEqual(
                    hashes[key], _sha16(msg.text),
                    "%r 指纹与正文不自洽：指纹 %s，正文实测 %s"
                    % (key, hashes[key][:16], _sha16(msg.text)[:16]))
                expected += 1
        self.assertEqual(len(hashes), expected,
                         "指纹条数 %d ≠ 正文条数 %d：有一边多读了轮次"
                         % (len(hashes), expected))

    def test_the_two_call_shape_really_goes_stale(self):
        """③ 旧形状（两次调用）在外部提交下**必然错版**。

        这一格的作用是让 ①② 有意义：它们只证明"这一次没撞上缝"，
        而这里证明缝**真的存在**、且外部一个正常写者就能造出来。

        外部写者按真实形状来：**同时**写 ``content`` 与 ``content_hash``。
        只改 ``content`` 不改指纹是"库被改坏"，由另一条通道抓，两件事不能混。
        """
        body_store = self._open()
        body_store.open()
        try:
            bodies = body_store.read_turns(1, 3)            # 快照 A：正文
        finally:
            body_store.close()

        foreign_text = "被外部驱动写过的第 3 轮提问"
        raw = sqlite3.connect(self.path, timeout=5.0)
        try:
            raw.execute("UPDATE turn SET content = ?, content_hash = ? "
                        "WHERE session_id = ? AND turn_no = 3 AND part = 0",
                        (foreign_text, _sha16(foreign_text), SESSION))
            raw.commit()
        finally:
            raw.close()

        hash_store = self._open()
        hash_store.open()
        try:
            hashes_after = hash_store.read_turns_with_hashes(1, 3)[1]   # 快照 B：指纹
        finally:
            hash_store.close()

        v1_text = bodies[2].user_text
        v2_hash = hashes_after[(3, 0)]
        self.assertNotEqual(v2_hash, _sha16(v1_text),
                            "外部改过之后指纹居然还对得上，这一格演示无效")
        self.assertEqual(v2_hash, _sha16(foreign_text), "第二份指纹没读到新版本")

    def test_port_has_no_hash_only_entry(self):
        """④ 误用面靠"拿不到那个输入"消失，不是靠禁令。"""
        store = self._open()
        try:
            for name in ("read_turn_hashes", "read_hashes", "turn_hashes"):
                self.assertFalse(hasattr(store, name),
                                 "端口上仍留着只读指纹的入口 %r：跨快照配对又变成可写的了"
                                 % (name,))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
