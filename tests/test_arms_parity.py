# -*- coding: utf-8 -*-
"""两臂对齐：A/B 唯一变量是装配方式，其余必须逐字节相同。

``bench`` 的每一条结论都建立在"两臂只差装配方式"之上。这条对齐不能靠人工检查，
只能靠断言（§4.14 的四条）：

1. 同 ``Policy`` / 同 ``SystemPrompt``（同 sha16）/ 同 ``TokenCounter``；
2. ``A.system_block().sha16 == B.system_block().sha16``；
3. 差异只允许出现在历史段：S 段与 U 段逐字节相同；
4. 只有 B 臂可能出现 ``boundary=True``。

第 3 条是本文件的核心：只要有一轮 U 段不同，"省了 X%"这个数就同时包含了
"装配差异"和"用户消息差异"，无法归因。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session  # noqa: E402
from ctxlattice.arms import (  # noqa: E402
    ArmProtocol,
    BaselineArm,
    LatticeArm,
    arm_summary,
    assert_arms_aligned,
)
from ctxlattice.model import BlockKind, Turn  # noqa: E402
from ctxlattice.session import make_system_prompt  # noqa: E402
from ctxlattice.tokens import make_counter  # noqa: E402

DELTA = [
    "T 品牌定位",
    "  P 视觉风格",
    "    R 主色用靛蓝  [约束]",
    "    R 圆角不超过 8px",
    "T 上线计划",
    "  P 里程碑",
    "    R 第 12 轮评审  [待办]",
    "    R 第 27 轮回访  [待办]",
]

TURNS = 12


def transcript() -> tuple[Turn, ...]:
    return tuple(
        Turn(index=i, user_text="第 %d 轮：讨论%s，请给出结论。" % (
            i, "靛蓝与墨绿的取舍" if i % 2 else "三个里程碑的排期"))
        for i in range(1, TURNS + 1)
    )


def build() -> tuple[ArmProtocol, ArmProtocol]:
    policy = Policy(compact_interval=4, verbatim_keep=2, budget_tokens=4000)
    system = make_system_prompt("你是后端架构师。\n只输出结论。\n")
    counter = make_counter(policy)
    baseline = BaselineArm(system=system, counter=counter, policy=policy)
    lattice = LatticeArm(
        store=MemoryStore(store_id="parity", top_text="把 C1 做完"),
        system=system, counter=counter, policy=policy,
    )
    return baseline, lattice


class TestArmsParity(unittest.TestCase):
    def test_system_block_identical(self):
        a, b = build()
        assert_arms_aligned(a, b)
        self.assertEqual(a.system_block().sha16, b.system_block().sha16)
        self.assertEqual(a.system_block().text, b.system_block().text)
        self.assertEqual(a.system_block().token_count, b.system_block().token_count)
        a.close()
        b.close()

    def test_difference_lives_only_in_history_segment(self):
        """S 段与 U 段逐字节相同；历史段允许不同（A 全量 / B = F+R）。"""
        a, b = build()
        for turn in transcript():
            ra = a.step(turn)
            rb = b.step(turn)

            sa, sb = ra.payload.block(BlockKind.S), rb.payload.block(BlockKind.S)
            ua, ub = ra.payload.block(BlockKind.U), rb.payload.block(BlockKind.U)
            self.assertEqual(sa.text, sb.text, "第 %d 轮 S 段不同" % turn.index)
            self.assertEqual(ua.text, ub.text, "第 %d 轮 U 段不同" % turn.index)

            a.observe("第 %d 轮回复。" % turn.index)
            b.observe("第 %d 轮回复。" % turn.index)
            if turn.index % 4 == 0:
                b.session.attribute(DELTA)
        a.close()
        b.close()

    def test_only_lattice_has_boundaries(self):
        a, b = build()
        for turn in transcript():
            ra = a.step(turn)
            rb = b.step(turn)
            self.assertFalse(ra.boundary, "A 臂永远不该有压缩边界")
            a.observe("好")
            b.observe("好")
            if turn.index % 4 == 0:
                b.session.attribute(DELTA)
        a.close()
        b.close()

    def test_lattice_saves_tokens_on_a_long_transcript(self):
        """方向性检查：B 臂累计 token 必须显著少于 A 臂。

        这不是 bench（bench 要算 ``cached_i`` 与 ``cost_eq``），只是一条
        "方案没写反"的冒烟断言。数值不作结论。
        """
        a, b = build()
        results_a, results_b = [], []
        for turn in transcript():
            results_a.append(a.step(turn))
            results_b.append(b.step(turn))
            a.observe("第 %d 轮回复，内容略长一些以体现全量重发的代价。" % turn.index)
            b.observe("第 %d 轮回复，内容略长一些以体现全量重发的代价。" % turn.index)
            if turn.index % 4 == 0:
                b.session.attribute(DELTA)
        a.close()
        b.close()

        total_a = sum(r.payload.token_count for r in results_a)
        total_b = sum(r.payload.token_count for r in results_b)
        self.assertLess(total_b, total_a,
                        "B 臂累计 token 不该大于 A 臂：%d vs %d" % (total_b, total_a))
        self.assertIn("边界", arm_summary(results_b))

    def test_reset_restores_from_store_not_from_memory(self):
        """``reset()`` 重开会话：已冻结的 F 与轮次缓冲从存储恢复（不删库）。"""
        a, b = build()
        for turn in transcript():
            b.step(turn)
            b.observe("好")
            if turn.index % 4 == 0:
                b.session.attribute(DELTA)
        before = b.session.inspect()
        b.reset()
        after = b.session.inspect()

        self.assertEqual(before.rev, after.rev)
        self.assertEqual(before.turn_hi, after.turn_hi)
        if before.plan is not None:
            self.assertIsNotNone(after.plan)
            self.assertEqual(before.plan.frozen_text, after.plan.frozen_text)
            self.assertEqual(before.plan.generation, after.plan.generation)
        a.close()
        b.close()

    def test_session_is_not_a_global_singleton(self):
        """P9：同一进程内可以是不同实例（但同一 session_id 不该被两个进程驱动）。"""
        one = Session.open(store=MemoryStore(store_id="s1"), system="A\n")
        two = Session.open(store=MemoryStore(store_id="s2"), system="A\n")
        self.assertIsNot(one, two)
        one.send("甲")
        two.send("乙")
        one.close()
        two.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
