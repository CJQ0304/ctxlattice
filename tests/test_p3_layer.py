"""P3（轮次连续性）挂在哪一层，决定走哪条失败矩阵条目。

这是本轮唯一一处「同一句断言，两个相反的期望」：

    挂 read_turns() / snapshot()  -> 抛 StoreCorruptError -> F1 -> send() 静默降级 buffer_only
    挂 open()                     -> 抛 StoreError        -> F2 -> 构造期大声失败

本设计采用**前者**。把断言挪到 `open()` **不是重构，是改契约**：
测试里期望抛异常的地方会变成静默降级，期望降级的地方会变成构造就炸——
两边都不报错地错位。

所以这里把两条都钉住：
* `open()` 不许因为「轮次有洞」而失败（那是 F1 的地盘）；
* `send()` 遇到有洞必须降级并记 `store_corrupt`，不许抛。

    python -m unittest tests.test_p3_layer -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctxlattice import MemoryStore, Payload, Policy, Session       # noqa: E402
from ctxlattice.errors import StoreCorruptError                    # noqa: E402

SYSTEM = "你是一个严谨的助手。"


class _HoleyStore(MemoryStore):
    """模拟「turn 表中间少了一轮」：装配期读轮次时才发现洞。"""

    holey = False

    def read_turns(self, lo, hi):                                   # type: ignore[override]
        if object.__getattribute__(self, "holey"):
            raise StoreCorruptError(
                "轮次不连续：第 3 轮缺失（read_turns 要求连续无洞）")
        return super().read_turns(lo, hi)


class TestP3LayerIsF1NotF2(unittest.TestCase):
    def _session(self) -> tuple[Session, _HoleyStore]:
        store = _HoleyStore()
        session = Session.open(store=store, policy=Policy(compact_interval=1,
                                                          verbatim_keep=1),
                               system=SYSTEM)
        # 先让库里有内容：空库走 no_compact（F9），永远不会到边界，也就测不到 F1
        session.attribute(["T 测试议题", "  P 子目的", "    R 一条要求"])
        for i in (1, 2, 3):
            session.send("第 %d 轮" % i)
            session.observe("收到 %d" % i)
        return session, store

    def test_open_on_healthy_store_does_not_raise(self):
        """健康库上 open() 不得抛 —— 洞是运行期故障（F1），不是配置错误（F2）。

        注意范围：这里只断言「健康库能打开」。洞在**打开那一刻就存在**属于未定义
        场景（正常生命周期里洞是中途出现的），不在本用例的契约内。
        """
        session, _ = self._session()
        again = Session.open(store=MemoryStore(), policy=Policy(), system=SYSTEM)
        self.assertIsNotNone(again)
        again.close()
        session.close()

    def test_send_degrades_with_store_corrupt(self):
        """有洞时 `send()` 必须降级成 buffer_only 并记 `store_corrupt`，而不是抛。"""
        session, store = self._session()
        object.__setattr__(store, "holey", True)
        payload = session.send("洞已经出现了，这一轮还得发出去。")

        # 这一轮**不该**是边界：存储健康探针（`_send` 的步骤 5'）在边界判定**之前**
        # 就读一次库，损坏在那一刻已被抓住，于是 ``_decide_boundary`` 见
        # ``not _readable`` 直接返回 NONE。
        #
        # 这里原本断言的是 ``payload.boundary is True``，理由是"非边界轮不读存储"——
        # 那句话在探针加进来之后就不成立了。而那个断言等价于要求"**先把这次压缩
        # 跑完再发现库是坏的**"，也就是白付一次全价轮去买一个已知答案。方向反了。
        self.assertFalse(payload.boundary,
                         "损坏应被探针在边界判定前拦住，不该白付一次压缩")
        self.assertFalse(session.stats().store_writable,
                         "损坏之后会话必须转入只读（不再尝试写盘/压缩）")

        self.assertIsInstance(payload, Payload)
        self.assertTrue(payload.degraded, "有洞必须标记降级，否则用户不知道上下文退化了")
        self.assertEqual(payload.degrade_reason, "store_corrupt")
        self.assertIn("洞已经出现了", payload.text, "用户本轮输入不可丢")

        reasons = [r.reason.value for r in session.degradation_log()]
        self.assertIn("store_corrupt", reasons)
        session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
