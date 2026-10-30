# -*- coding: utf-8 -*-
"""计费三层的算术闸门（MEASUREMENT-SPEC §3）。

这个模块只钉**算术**，不碰会话、不碰存储 —— 因为它守的是整个项目对外说话的
那一个数。数字错了不会抛异常，只会让报告变得好看。

## 为什么单独立一个套件

这里唯一的真实缺陷（已实测踩中）是：估算器路径拿**字节**公共前缀去除
`chars_per_token`，而 `count()` 数的是**字符**。中文 UTF-8 是 3 字节/字，
于是 `cached` 被系统性放大到约 3 倍，再被 `min(·, gross)` 截到 `gross`，
`billable` 变成 0 ——

* 每一轮的"缓存命中"都显示为 100%，包括**边界轮**（前缀本该全废）；
* δ 敏感性表塌成「成本降幅 == 输入降幅」，δ=0.1 那一档从负数翻成正数。

也就是说：**修不修这个 bug，报告会讲出两个相反的结论，而且错的那个更好听。**

所以下面的断言不写"能跑通"，只写"哪种结果算错"。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice.bench.metrics import (                       # noqa: E402
    DELTA_GRID,
    cache_prefix_tokens,
    measure_turn,
)
from ctxlattice.tokens import EstimateCounter                # noqa: E402


class _P:
    """最小 payload 替身：计费只用到 ``text`` 与 ``token_count``。"""

    def __init__(self, text: str, counter: EstimateCounter) -> None:
        self.text = text
        self.token_count = counter.count(text)


CN = "上下文压缩与缓存是替代品。"          # 每条 13 字，含中文标点


class TestCachePrefixUnit(unittest.TestCase):
    """估算器路径必须与 ``count()`` 同单位（字符），不是字节。"""

    def setUp(self) -> None:
        self.c = EstimateCounter()

    def test_estimator_counts_chars_not_bytes(self):
        """纯追加：命中长度必须约等于上一轮的长度，而不是它的三倍。"""
        prev_text = CN * 20                              # 260 字符
        cur_text = prev_text + CN * 20                   # 520 字符
        prev, cur = _P(prev_text, self.c), _P(cur_text, self.c)

        cached = cache_prefix_tokens(prev, cur, self.c)

        self.assertEqual(self.c.count(prev_text), 130)
        self.assertEqual(self.c.count(cur_text), 260)
        # 纯追加时，命中长度就是上一轮的全部；允许 ±1 的取整差
        self.assertAlmostEqual(cached, 130, delta=1.0)
        # 关键的反向断言：旧实现返回 390（字节 780 / 2），进而被截成 260
        self.assertLess(cached, self.c.count(cur_text),
                        "纯追加轮不可能 100% 命中（本轮新增的那段没被缓存过）")

    def test_pure_append_bills_exactly_the_new_part(self):
        """**语义不变量**：纯追加轮的可计费量 == 本轮新增的 token 数。"""
        prev = _P(CN * 20, self.c)
        cur = _P(CN * 20 + "这是本轮新增的一句。", self.c)
        row = measure_turn(arm="B", turn=2, payload=cur, prev=prev, counter=self.c,
                           delta=0.1, price=1.0, boundary=False)
        added = cur.token_count - prev.token_count
        self.assertAlmostEqual(row.billable, added, delta=2.0,
                               msg="纯追加轮的 billable 应当只剩新增部分")

    def test_rewrite_collapses_cache(self):
        """边界轮前缀被重写 → 命中量必须塌到很小，billable 接近 gross。"""
        prev = _P(CN * 40, self.c)
        # 同一段业务文本，但开头整块换掉了（模拟 F 重建后 S/F 段变了）
        cur = _P("（新的冻结块）" + CN * 40, self.c)
        cached = cache_prefix_tokens(prev, cur, self.c)
        self.assertLessEqual(cached, 8, "开头就分叉了，命中不该超过一个短语")

        row = measure_turn(arm="B", turn=9, payload=cur, prev=prev, counter=self.c,
                           delta=0.1, price=1.0, boundary=True)
        self.assertGreater(row.billable, row.gross_input * 0.9,
                           "边界轮几乎整包按全价付 —— 这正是 δ 敏感性表的来源")

    def test_no_prev_is_cold_start(self):
        self.assertEqual(cache_prefix_tokens(None, _P(CN, self.c), self.c), 0)

    def test_cached_never_exceeds_gross(self):
        """护栏本身也要钉住：``cached > gross`` 会让 billable 变负、成本变负。"""
        c = EstimateCounter()
        for prev_text, cur_text in (
            (CN * 30, CN * 30),
            (CN * 30, CN),
            ("", CN * 5),
            (CN * 5, ""),
        ):
            row = measure_turn(arm="A", turn=1, payload=_P(cur_text, c),
                               prev=_P(prev_text, c), counter=c,
                               delta=0.1, price=1.0, boundary=False)
            self.assertGreaterEqual(row.cached, 0)
            self.assertLessEqual(row.cached, row.gross_input)
            self.assertGreaterEqual(row.billable, 0)
            self.assertGreaterEqual(row.cost_eq, 0.0)


class TestCostFormula(unittest.TestCase):
    """`cost_eq = billable·p + cached·p·δ`，且 δ=1.0 时退化为 `gross·p`。"""

    def setUp(self) -> None:
        self.c = EstimateCounter()
        self.prev = _P(CN * 10, self.c)
        self.cur = _P(CN * 10 + CN, self.c)

    def _row(self, delta: float, price: float = 1.0):
        return measure_turn(arm="B", turn=2, payload=self.cur, prev=self.prev,
                            counter=self.c, delta=delta, price=price, boundary=False)

    def test_formula(self):
        row = self._row(0.1, price=2.0)
        expected = row.billable * 2.0 + row.cached * 2.0 * 0.1
        self.assertAlmostEqual(row.cost_eq, expected, places=6)

    def test_delta_one_equals_gross(self):
        row = self._row(1.0)
        self.assertAlmostEqual(row.cost_eq, float(row.gross_input), places=6)

    def test_delta_zero_equals_billable(self):
        row = self._row(0.0)
        self.assertAlmostEqual(row.cost_eq, float(row.billable), places=6)

    def test_cost_is_monotone_in_delta(self):
        """δ 越大，成本越高（缓存越不值钱）—— 敏感性表的形状靠这条成立。"""
        costs = [self._row(d).cost_eq for d in DELTA_GRID]
        self.assertEqual(costs, sorted(costs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
