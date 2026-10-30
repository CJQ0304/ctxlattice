# -*- coding: utf-8 -*-
"""SQLite 存储层的四类"静默出错"点。

这些错误的共同特征：**不报错**。库照建、测试照绿，只是数据或成本悄悄错掉。
所以每条都写成"反向断言"—— 先构造能触发它的最小场景，再断言它没发生。

1. **改标记被 upsert 吞掉 / 把 ``[钉住]`` 清空**（R2）：两个方向都不报错。
   ``INSERT OR IGNORE`` → ``[已废]`` 永远打不上；
   ``SET tag = excluded.tag`` → 用户明确钉住的内容被老化折叠掉。
   唯一正确写法：``SET tag = COALESCE(excluded.tag, leaf.tag)``。
2. **压缩让 ``rev`` 变了**（I16）：``commit_plan`` 里顺手写 ``rev = rev + 1``
   读起来完全说得通，但此后每次 no-op 判定都拿错基准。
3. **``plans`` 覆盖而不是追加**（P8）：重压必须新增 generation，
   ``snapshot()`` 取 generation 最大的一行。
4. **轮次有洞却读得出来**（P3）：读时必须有断言，否则 R 段会拼出一段假历史。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session, SqliteStore  # noqa: E402
from ctxlattice.errors import SessionConflictError, StoreCorruptError  # noqa: E402
from ctxlattice.model import Turn  # noqa: E402


class _TempDb(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "store.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, **kwargs) -> SqliteStore:
        return SqliteStore(self.path, store_id="t", session_id="s1",
                           transcript_hash="b" * 64, **kwargs)


class TestLeafTagUpsert(_TempDb):
    """R2：改标记的两个方向。"""

    def test_untagged_rewrite_keeps_pinned_tag(self):
        """先写 ``[钉住]``，再写同文本**无标记** → ``[钉住]`` 必须还在。"""
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [钉住]"])
            txn.commit()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝"])
            txn.commit()

        leaf = store.snapshot().topics[0].purposes[0].leaves[0]
        store.close()
        self.assertEqual(leaf.tag, "钉住",
                         "无标记的重复行把已有的 [钉住] 清空了 —— 静默数据丢失")

    def test_new_tag_is_applied(self):
        """同文本 + 新标记 = 改标记（不是 no-op）。"""
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [最新]"])
            txn.commit()
        with store.begin(write=True) as txn:
            stat = txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [已废]"])
            txn.commit()

        leaf = store.snapshot().topics[0].purposes[0].leaves[0]
        store.close()
        self.assertEqual(stat.r_retag, 1)
        self.assertEqual(leaf.tag, "已废",
                         "[已废] 打不上意味着 prune --drop-stale 永远没有条目可删")

    def test_e_leaf_tag_also_uses_coalesce(self):
        store = self.store()
        store.open()
        e_line = "E 接口文档 | src:docs/api.md | bytes:12 | sha256:0123456789abcdef"
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 上线计划", "  P 依赖", "    " + e_line + "  [钉住]"])
            txn.commit()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 上线计划", "  P 依赖", "    " + e_line])
            txn.commit()
        leaf = store.snapshot().topics[0].purposes[0].leaves[0]
        store.close()
        self.assertEqual(leaf.tag, "钉住")
        self.assertEqual(leaf.src, "docs/api.md")

    def test_pure_duplicate_is_a_noop_and_keeps_rev(self):
        """纯重复 → r_dup、changed=False、**rev 不动**（P4）。"""
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝"])
            txn.commit()
        rev_before = store.snapshot().rev
        with store.begin(write=True) as txn:
            stat = txn.apply_delta(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝"])
            txn.commit()
        rev_after = store.snapshot().rev
        store.close()

        self.assertEqual(stat.r_dup, 1)
        self.assertFalse(stat.changed)
        self.assertEqual(rev_before, rev_after, "no-op 不该让 rev 前进")


class TestRevInvariants(_TempDb):
    """I16：压缩不改变 ``rev``。"""

    def test_compaction_does_not_touch_rev(self):
        store = self.store()
        session = Session.open(
            store=store, policy=Policy(compact_interval=2, verbatim_keep=1),
            system="你是后端架构师。\n")
        session.attribute(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [约束]"])
        rev_before = session.inspect().rev

        for i in range(1, 7):
            session.send("第 %d 轮" % i)
            session.observe("好")
        rev_after = session.inspect().rev
        generations = session.inspect().plan.generation
        session.close()

        self.assertGreaterEqual(generations, 2, "本会话应当压过多次")
        self.assertEqual(rev_before, rev_after,
                         "压缩改变了 rev（I16）：此后每次 no-op 判定都会拿错基准")

    def test_attribute_does_bump_rev_only_when_content_changes(self):
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位"])
            txn.commit()
        first = store.snapshot().rev
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位"])
            txn.commit()
        second = store.snapshot().rev
        with store.begin(write=True) as txn:
            txn.apply_delta(["T 品牌定位  [钉住]"])
            txn.commit()
        third = store.snapshot().rev
        store.close()

        self.assertEqual(first, 1)
        self.assertEqual(second, 1, "重复内容不该让 rev 前进")
        self.assertEqual(third, 2, "改标记进了指纹，就算改内容")


class TestPlansAppend(_TempDb):
    """P8：按 generation 追加、永不覆盖、取最大 generation。"""

    def test_two_generations_coexist_and_latest_wins(self):
        store = self.store()
        session = Session.open(
            store=store, policy=Policy(compact_interval=2, verbatim_keep=1),
            system="你是后端架构师。\n")
        session.attribute(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝"])
        for i in range(1, 5):
            session.send("第 %d 轮" % i)
            session.observe("好")
        session.close()

        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT generation, frozen_sha16, length(frozen_text) FROM plans"
            " ORDER BY generation").fetchall()
        conn.close()

        self.assertGreaterEqual(len(rows), 2, "重压必须新增 generation 而不是覆盖")
        self.assertEqual([r[0] for r in rows], sorted({r[0] for r in rows}))
        for _generation, _sha, size in rows:
            self.assertGreaterEqual(size, 1, "frozen_text 必须落库（P8）")

    def test_corrupted_frozen_text_is_reported_not_repaired(self):
        """``frozen_sha16`` 自洽校验：不符即 ``StoreCorruptError``，不静默修。"""
        store = self.store()
        session = Session.open(
            store=store, policy=Policy(compact_interval=2, verbatim_keep=1),
            system="你是后端架构师。\n")
        session.attribute(["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝"])
        for i in range(1, 4):
            session.send("第 %d 轮" % i)
            session.observe("好")
        session.close()

        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE plans SET frozen_text = frozen_text || 'x'"
                     " WHERE generation = (SELECT MAX(generation) FROM plans)")
        conn.commit()
        conn.close()

        store2 = self.store()
        with self.assertRaises(StoreCorruptError) as ctx:
            store2.snapshot()
        self.assertIn("冻结块", str(ctx.exception))


class TestWritePathParity(_TempDb):
    """两个后端在写路径上必须**逐点一致** —— 相反的行为不会以任何形式报错。

    异向的后果不是"一边多一条检查"，而是**同一段调用在两个后端上语义不同**，
    而 bench 的数字与用例都还各自绿着（``MemoryStore`` 的注释就写着这句：
    「两版不一致时，bench 的数字不可比，而差异不会以任何形式报错」）。
    """

    def test_shrinking_replies_never_deletes_history(self):
        """端口契约「回复只许按 part 追加」：带回来的回复变少，不等于可以删历史。"""
        from ctxlattice.model import Message, Role

        def scenario(store):
            store.open()
            with store.begin(write=True) as txn:
                txn.put_turn(Turn(index=1, user_text="问", replies=(
                    Message(Role.ASSISTANT, "答一"), Message(Role.TOOL, "工具输出"))))
                txn.commit()
            with store.begin(write=True) as txn:
                # 调用方拿了个**旧的** Turn 对象回来（回复还没攒齐）
                txn.put_turn(Turn(index=1, user_text="问"))
                txn.commit()
            kept = [m.text for m in store.read_turns(1, 1)[0].replies]
            store.close()
            return kept

        memory = scenario(MemoryStore(store_id="m", session_id="s"))
        sqlite_kept = scenario(self.store())

        self.assertEqual(memory, ["答一", "工具输出"],
                         "内存版把已登记的回复静默删掉了：装配看不见（它读进程内缓冲），"
                         "重启之后才显形")
        self.assertEqual(memory, sqlite_kept, "两个后端在「回复变少」上给出了不同结果")


class TestTurnContinuity(_TempDb):
    """P3：轮次有洞必须在读时炸，而不是拼出一段假历史。"""

    def test_hole_is_a_store_corrupt_error(self):
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            for i in range(1, 5):
                txn.put_turn(Turn(index=i, user_text="第 %d 轮" % i))
            txn.commit()
        store.close()

        conn = sqlite3.connect(self.path)
        conn.execute("DELETE FROM turn WHERE turn_no = 3")
        conn.commit()
        conn.close()

        store2 = self.store()
        with self.assertRaises(StoreCorruptError):
            store2.read_turns(1, 4)
        with self.assertRaises(StoreCorruptError):
            store2.snapshot()          # 全库聚合断言
        store2.close()

    def test_turn_shape_is_user_plus_replies(self):
        """一轮 = 1 条 user + 其后若干条 assistant/tool；不拍平成两列。"""
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.put_turn(Turn(index=1, user_text="问"))
            txn.commit()
        from ctxlattice.model import Message, Role

        with store.begin(write=True) as txn:
            txn.put_turn(Turn(index=1, user_text="问", replies=(
                Message(Role.ASSISTANT, "答一"),
                Message(Role.TOOL, "工具输出"),
            )))
            txn.commit()
        turns = store.read_turns(1, 1)
        store.close()

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_text, "问")
        self.assertEqual([m.role for m in turns[0].replies], [Role.ASSISTANT, Role.TOOL])

    def test_forked_content_is_rejected(self):
        """同一 ``(turn_no, part)`` 的字节变了 → ``SessionConflictError``（F16 的签名）。

        为什么不是 ``StoreCorruptError``：这一支**被正确输入撞得到** —— 两个
        ``Session`` 写同一条轮次链时，各轮内容不同，先到的已落库，后到的必撞这里。
        译成"库损坏"会让运维去跑 ``integrity_check``（一定全绿），真处置
        （"这个 session 有两个驱动"）永远不会被执行。
        """
        store = self.store()
        store.open()
        with store.begin(write=True) as txn:
            txn.put_turn(Turn(index=1, user_text="原始问题"))
            txn.commit()
        with self.assertRaises(SessionConflictError):
            with store.begin(write=True) as txn:
                txn.put_turn(Turn(index=1, user_text="被改写的问题"))
                txn.commit()
        store.close()

    def test_skipping_a_turn_number_is_rejected(self):
        """跳号留 ``StoreCorruptError``：它可由**外部改库**（回拨 ``turn_seq``）造成。

        与上一条的对比就是判据本身：*同形状的断言，可达输入集不同，译法就不同。*
        """
        store = self.store()
        store.open()
        with self.assertRaises(StoreCorruptError):
            with store.begin(write=True) as txn:
                txn.put_turn(Turn(index=5, user_text="跳号"))
                txn.commit()
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
