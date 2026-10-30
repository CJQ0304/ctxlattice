# -*- coding: utf-8 -*-
"""I1：R 段是"边界内只追加"，不是每轮滑动的窗口。

这是整套设计的**唯一前提**。它被破坏时：
每一轮与上一轮在 R 段起点就分叉 → ``cached_i`` 退化为 ``count(S)`` →
每一轮都是全价轮 → B 臂在两项指标上同时劣于 A 臂，而**没有任何异常**。

所以本文件不测"函数返回值对不对"，只测一件事：
**``cur.to_bytes().startswith(prev.to_bytes())``**。
这条断言比任何单测都值钱 —— 它直接测的是前缀命中率的物理基础。

跑法：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session  # noqa: E402
from ctxlattice.assembler import (  # noqa: E402
    assert_prefix_stable,
    common_prefix_bytes,
    prefix_violation,
)

SYSTEM = "你是后端架构师。\n只输出结论，不要复述需求。\n"

DELTA = [
    "T 品牌定位",
    "  P 视觉风格",
    "    R 主色用靛蓝  [约束]",
    "    R 圆角不超过 8px",
    "T 上线计划",
    "  P 里程碑",
    "    R 第 12 轮评审  [待办]",
]


def make_session(policy: Policy | None = None) -> Session:
    store = MemoryStore(store_id="prefix", top_text="把 C1 做完")
    return Session.open(
        store=store,
        policy=policy or Policy(compact_interval=3, verbatim_keep=2),
        system=SYSTEM,
    )


class TestPrefixStability(unittest.TestCase):
    """五个连续的非边界轮，逐字节前缀关系必须成立。"""

    def test_five_consecutive_non_boundary_turns(self):
        # N=100 → 整个测试里只会有一个 COLD_START 边界（第 1 轮），
        # 之后全是非边界轮。这样断言不被"边界轮允许破坏前缀"干扰。
        session = make_session(Policy(compact_interval=100, verbatim_keep=3))
        payloads = []
        for i in range(1, 6):
            payloads.append(session.send("第 %d 轮：继续。" % i))
            session.observe("收到。")
        session.close()

        self.assertEqual(len(payloads), 5)
        for index, (prev, cur) in enumerate(zip(payloads, payloads[1:]), start=2):
            self.assertIsNone(prefix_violation(prev, cur),
                              "第 %d 轮（非边界）前缀关系被破坏" % index)
            self.assertTrue(cur.to_bytes().startswith(prev.to_bytes()))

    def test_common_prefix_is_the_whole_previous_payload(self):
        """公共前缀 = 上一轮 payload 全长（不是"近似相等"）。"""
        session = make_session(Policy(compact_interval=100, verbatim_keep=3))
        prev = session.send("第一轮")
        session.observe("好")
        cur = session.send("第二轮")
        session.close()

        self.assertEqual(common_prefix_bytes(prev.to_bytes(), cur.to_bytes()),
                         len(prev.to_bytes()))

    def test_prefix_holds_across_boundaries_except_the_boundary_turn(self):
        """有边界的会话里，**所有非边界轮**都满足前缀；边界轮单独记账。"""
        session = make_session(Policy(compact_interval=3, verbatim_keep=2))
        # 先让库里有内容：否则前几轮会走 F9（空库不压缩），测不到边界行为。
        session.attribute(DELTA)
        prev = None
        checked = 0
        boundary_turns = []
        degraded_turns = []
        for i in range(1, 15):
            payload = session.send("第 %d 轮：讨论靛蓝与墨绿。" % i)
            session.observe("好的。")
            if i % 3 == 0:
                session.attribute(DELTA)
            if payload.degraded:
                degraded_turns.append(i)
            if payload.boundary:
                boundary_turns.append(i)
            elif prev is not None and not payload.degraded and not prev.degraded:
                self.assertIsNone(prefix_violation(prev, payload),
                                  "第 %d 轮（非边界）前缀断裂" % i)
                checked += 1
            prev = payload
        session.close()

        # 断言的是「所有非边界轮都验过」；降级轮若被静默跳过，这个测试会变得没有效力，
        # 所以先禁止降级，再要求 checked 等于理论值。
        self.assertEqual(degraded_turns, [], "降级轮绕过了前缀断言，测试失效")
        self.assertEqual(checked, 14 - len(boundary_turns),
                         "非边界轮数不符：说明有轮次被跳过，测试没测全")
        # 第 1 轮是 COLD_START（库已有内容 → 建第一代 plan），之后 3/6/9/12 按 N=3 触发。
        # 边界是 **epoch 相对**的（turn_index - plan.epoch_turn >= N），
        # 不是绝对轮次的整数倍。冷启动在第 1 轮建 plan，之后每 3 轮一次 → 4/7/10/13。
        self.assertEqual(boundary_turns, [1, 4, 7, 10, 13])
        self.assertGreaterEqual(checked, 8, "非边界轮太少，这个测试没测到东西")

    def test_r_first_turn_is_a_segment_start_not_a_sliding_window(self):
        """``K`` 是段起点：边界之间 ``r_first_turn`` 恒定；换段只在边界发生。

        若把它实现成 ``turns[-K:]``（滑动窗口），这里会看到 R 段起点每轮 +
        1 —— 而 payload 前缀随之每轮错位。
        """
        session = make_session(Policy(compact_interval=4, verbatim_keep=2))
        seen = []
        for i in range(1, 11):
            session.send("第 %d 轮" % i)
            session.observe("好")
            session.attribute(DELTA)
            snap = session.inspect()
            if snap.plan is not None:
                seen.append((i, snap.plan.epoch_turn, snap.plan.r_first_turn))
        session.close()

        by_epoch = {}
        for turn, epoch, r_first in seen:
            by_epoch.setdefault(epoch, set()).add(r_first)
        for epoch, r_firsts in by_epoch.items():
            self.assertEqual(len(r_firsts), 1,
                             "epoch=%d 期间 r_first_turn 变过：%s（说明 R 段在滑动）"
                             % (epoch, sorted(r_firsts)))

    def test_assert_prefix_stable_raises_with_location(self):
        """破坏时必须炸得足够响，且指出是哪个块的第几字节。"""
        session = make_session(Policy(compact_interval=100, verbatim_keep=3))
        first = session.send("第一轮")
        session.observe("好")
        second = session.send("第二轮")
        session.close()

        assert_prefix_stable(first, second)          # 不该抛

        from ctxlattice.errors import InvariantError

        with self.assertRaises(InvariantError) as ctx:
            assert_prefix_stable(second, first)      # 反序必然不是前缀
        message = str(ctx.exception)
        self.assertIn("I1", message)
        self.assertIn("公共前缀", message)

    def test_violation_points_at_the_block_and_offset(self):
        """中文报错要能直接定位到代码：指出哪个块、块内第几字节。"""
        from ctxlattice.assembler import _describe_offset
        from ctxlattice.model import BlockKind
        from ctxlattice.model import Payload, make_block

        payload = Payload(
            blocks=(make_block(BlockKind.S, "system\n", 1),
                    make_block(BlockKind.U, "user: hi\n", 1)),
            counter_id="test",
        )
        detail = _describe_offset(payload, 2)
        self.assertIn("块 S", detail)
        self.assertIn("内偏移 2", detail)
        tail = _describe_offset(payload, 10_000)
        self.assertIn("越过末尾", tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
