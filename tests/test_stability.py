"""稳定性与不变量的常开回归。

这五条守的都是「不报错但会让方案失效」的那类问题，所以必须常开，不能只在验收时跑一次。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctxlattice import MemoryStore, Payload, Policy, Session       # noqa: E402
from ctxlattice.bench.transcripts import load_transcript            # noqa: E402

TRANSCRIPT = ROOT / "bench" / "transcripts" / "good-repo-evolution.jsonl"


def _drive(session: Session, turns) -> list[bytes]:
    out = []
    for t in turns:
        p = session.send(t.user_text)
        out.append(p.to_bytes())
        for m in t.replies:
            session.observe(m.text, role=m.role)
        lines = _attrib(t.index)
        if lines:
            session.attribute(list(lines))
    return out


def _attrib(turn: int) -> list[str]:
    tr = load_transcript(TRANSCRIPT)
    return list(tr.attrib_for(turn))


class TestStability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = load_transcript(TRANSCRIPT)

    # 1 确定性 ---------------------------------------------------------------
    def test_determinism_bytes(self):
        """同输入两遍必须逐字节相同 —— 否则「可复现」是空话。"""
        a = _drive(Session.open(store=MemoryStore(), policy=Policy()),
                   self.tr.turns[:20])
        b = _drive(Session.open(store=MemoryStore(), policy=Policy()),
                   self.tr.turns[:20])
        self.assertEqual(a, b)

    # 2 前缀稳定（I1）--------------------------------------------------------
    def test_prefix_stability_non_boundary(self):
        """非边界轮必须以上一轮为字节前缀。

        这是整个方案成立的前提：一旦每轮重写，缓存全失效，省下的 token 钱
        不够赔缓存损失，而且**不会报任何错**。
        """
        s = Session.open(store=MemoryStore(), policy=Policy())
        prev = None
        bounds = 0
        for t in self.tr.turns:
            p = s.send(t.user_text)
            if p.boundary:
                bounds += 1
            elif prev is not None:
                self.assertTrue(
                    p.to_bytes().startswith(prev.to_bytes()),
                    "第 %d 轮不是上一轮的前缀 —— 缓存必然失效" % t.index)
            prev = p
            for m in t.replies:
                s.observe(m.text, role=m.role)
            lines = list(self.tr.attrib_for(t.index))
            if lines:
                s.attribute(lines)
        self.assertGreater(bounds, 0, "40 轮内应至少有一次压缩边界")
        self.assertLess(bounds, len(self.tr.turns), "不许每轮都是边界轮")

    # 3 A7：归因不得触发边界 -------------------------------------------------
    def test_attribution_does_not_trigger_boundary(self):
        """`attribute()` 改变内容后，下一轮不得变成边界轮。

        这是 A7「审计字段不得参与控制流」的可执行形态。若退化，每轮都是全价轮，
        而表面上一切正常。
        """
        s = Session.open(store=MemoryStore(), policy=Policy())
        for t in self.tr.turns[:10]:
            s.send(t.user_text)
            for m in t.replies:
                s.observe(m.text, role=m.role)
            lines = list(self.tr.attrib_for(t.index))
            if lines:
                s.attribute(lines)
        p = s.send(self.tr.turns[10].user_text)
        self.assertFalse(p.boundary, "归因把 plan 判失效了 —— 每轮都会重压")

    # 4 fail-open ------------------------------------------------------------
    def test_failopen_on_store_failure(self):
        """存储中途失效时，`send()` 必须仍返回 payload（且诚实标记 degraded）。"""

        class LateBoom(MemoryStore):
            calls = 0

            def snapshot(self):
                LateBoom.calls += 1
                if LateBoom.calls > 2:
                    raise RuntimeError("存储中途炸了")
                return super().snapshot()

        s = Session.open(store=LateBoom(), policy=Policy())
        s.send("第一轮正常")
        p = s.send("第二轮开始炸")
        self.assertIsInstance(p, Payload)
        self.assertTrue(p.degraded)

    # 5 超预算不截断用户输入 -------------------------------------------------
    def test_over_budget_does_not_truncate_user_input(self):
        """单条用户消息本身超预算时，照发，绝不截断。"""
        s = Session.open(store=MemoryStore(), policy=Policy(budget_tokens=2000))
        big = "长" * 6000
        p = s.send(big)
        self.assertIn(big, p.text)
        self.assertGreater(p.token_count, 2000)

    def test_over_budget_is_marked_on_the_payload(self):
        """F4 必须**同时**记事件与标 payload —— 只记事件等于用户不知情。

        §10.5 明确写了「不产生带 `degraded` payload 的只有 F5 与 F14」，F4 不在其中。
        原来的实现只 `_record()` 了一条 `BUDGET_OVERFLOW`，payload 上 `degraded=False`：
        报告里有一条降级、而调用方一无所知，两边对不上且没有任何一层会报错。
        """
        s = Session.open(store=MemoryStore(), policy=Policy(budget_tokens=2000))
        p = s.send("长" * 6000)
        self.assertTrue(p.degraded, "超预算必须在 payload 上标出来")
        self.assertEqual(p.degrade_reason, "budget_overflow")
        self.assertIn("budget_overflow", [r.reason.value for r in s.degradation_log()])

    def test_over_budget_marking_does_not_change_a_byte(self):
        """标 `degraded` **不得**改变正文：它只进 `flags()`，不参与渲染。

        这条是给"事后 `replace()` 会不会破坏确定性"上的保险 —— 会的话，
        前缀缓存就会在这一轮莫名其妙断掉，而且原因看起来在别处。
        """
        s1 = Session.open(store=MemoryStore(), policy=Policy(budget_tokens=2000))
        p1 = s1.send("长" * 6000)
        before = p1.text
        self.assertTrue(p1.degraded)
        # 同一份输入在**宽松预算**下重跑：正文必须逐字节相同
        s2 = Session.open(store=MemoryStore(), policy=Policy(budget_tokens=200000))
        p2 = s2.send("长" * 6000)
        self.assertFalse(p2.degraded)
        self.assertEqual(before, p2.text,
                         "标 degraded 改变了正文 —— 那前缀会在这一轮无故断裂")


if __name__ == "__main__":
    unittest.main(verbosity=2)
