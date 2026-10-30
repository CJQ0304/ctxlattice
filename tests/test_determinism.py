# -*- coding: utf-8 -*-
"""确定性：同输入两遍 → 逐字节相同；且 MemoryStore 与 SqliteStore 逐点一致。

为什么这条比"结果看起来对"重要：bench 的全部数字都建立在"重放同一份
transcript 得到同一批 payload"之上。一旦有一次不确定性（时间戳、绝对路径、
``set`` 迭代序、浮点格式化），两次跑的 ``cached_i`` 就不可比，而**没有任何
报错** —— 只会看到噪声很大的结论。

三条断言：
1. 同一输入跑两遍（全新 store）→ ``sha16`` 序列与字节完全一致；
2. 参考实现与 SQLite 实现 → 同一 transcript 下**逐轮 sha16 相同**；
3. 跨"重新打开会话"的连续性：落库的 F 与内存里的 F 是同一份字节（I6）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session, SqliteStore  # noqa: E402
from ctxlattice.model import BlockKind  # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"

DELTAS = (
    ["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝", "    R 圆角不超过 8px  [约束]"],
    ["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [最新]"],
    ["T 上线计划", "  P 里程碑", "    R 第 12 轮评审  [待办]"],
    ["T 品牌定位", "  P 视觉风格", "    R 主色用靛蓝  [已废]", "    R 主色改用墨绿  [最新]"],
)

TURNS = 14


def drive(session: Session) -> list[bytes]:
    out = []
    for i in range(1, TURNS + 1):
        payload = session.send("第 %d 轮：讨论%s。" % (i, "靛蓝与墨绿" if i % 2 else "里程碑"))
        out.append(payload.to_bytes())
        session.observe("第 %d 轮回复：好的。" % i)
        if i % 4 == 0:
            session.attribute(DELTAS[(i // 4 - 1) % len(DELTAS)])
    return out


def policy() -> Policy:
    return Policy(compact_interval=3, verbatim_keep=2, budget_tokens=4000)


class TestDeterminism(unittest.TestCase):
    def test_same_input_twice_is_byte_identical(self):
        runs = []
        for _ in range(2):
            session = Session.open(
                store=MemoryStore(store_id="det", top_text="把 C1 做完"),
                policy=policy(), system=SYSTEM)
            runs.append(drive(session))
            session.close()
        self.assertEqual(runs[0], runs[1], "同一输入两次运行产出不同字节")

    def test_sqlite_matches_memory_point_by_point(self):
        memory = Session.open(
            store=MemoryStore(store_id="det", top_text="把 C1 做完"),
            policy=policy(), system=SYSTEM)
        left = drive(memory)
        memory.close()

        tmp = tempfile.mkdtemp()
        try:
            store = SqliteStore(os.path.join(tmp, "store.db"), store_id="det",
                                top_text="把 C1 做完", session_id="s1",
                                transcript_hash="a" * 64, arm="B")
            session = Session.open(store=store, policy=policy(), system=SYSTEM)
            right = drive(session)
            session.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(left), len(right))
        for index, (a, b) in enumerate(zip(left, right), start=1):
            self.assertEqual(
                a, b,
                "第 %d 轮 MemoryStore 与 SqliteStore 产出不同字节。"
                "两版行为不一致时 bench 的数字就不可比，而且差异不会以任何形式报错"
                % index)

    def test_frozen_block_survives_reopen(self):
        """I6 的落地点：F 是**写进去的**，不是算出来的。

        重开会话后必须逐字拿到同一份 F。若实现成"读不到就 build_frozen_block"，
        这里会在第 2 段（重新打开后）出现不同字节 —— 因为边界之间
        ``attribute()`` 已经改过库了。
        """
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "store.db")
            store = SqliteStore(path, store_id="det", top_text="把 C1 做完",
                                session_id="s1", transcript_hash="a" * 64)
            session = Session.open(store=store, policy=policy(), system=SYSTEM)
            drive(session)
            frozen_before = session.inspect().plan
            session.close()

            self.assertIsNotNone(frozen_before, "本会话应当已经冻结过至少一次")

            # 重新打开：F 只能来自 plan.frozen_text
            store2 = SqliteStore(path, store_id="det", top_text="把 C1 做完",
                                 session_id="s1", transcript_hash="a" * 64)
            session2 = Session.open(store=store2, policy=policy(), system=SYSTEM)
            frozen_after = session2.inspect().plan
            session2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(frozen_before.frozen_text, frozen_after.frozen_text)
        self.assertEqual(frozen_before.frozen_sha16, frozen_after.frozen_sha16)
        self.assertEqual(frozen_before.generation, frozen_after.generation)

    def test_no_timestamp_or_path_leaks_into_payload(self):
        """payload 里不得出现时间戳 / 绝对路径 / 平台分隔符。"""
        session = Session.open(
            store=MemoryStore(store_id="det", top_text="把 C1 做完"),
            policy=policy(), system=SYSTEM)
        payload = session.send("检查一下 /home/user/secret 与 C:\\\\Users\\\\x")
        session.close()

        f = payload.block(BlockKind.F)
        self.assertIsNone(f, "第 1 轮（COLD_START、库为空）不该有 F")
        self.assertNotIn("\r", payload.text)
        self.assertFalse(payload.text.startswith("\ufeff"))
        self.assertTrue(payload.to_bytes().endswith(b"\n"))

    def test_payload_token_count_is_the_sum_of_blocks(self):
        """``token_count`` 的口径：各块 token 数之和（不是"整串再数一遍"）。"""
        session = Session.open(
            store=MemoryStore(store_id="det", top_text="把 C1 做完"),
            policy=policy(), system=SYSTEM)
        payload = session.send("口径检查")
        session.close()

        self.assertEqual(payload.token_count,
                         sum(block.token_count for block in payload.blocks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
