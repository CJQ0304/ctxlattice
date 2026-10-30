# -*- coding: utf-8 -*-
"""I3 的第三道闸门：**非边界轮的存储读次数必须是个常数。**

## 为什么要有这一条

`assembler` 不 import `freeze`、`AssemblyInputs.frozen` 只声明 `text` 一个成员——
这已经挡住"装配器自己去构建 F"。但它挡不住**下面这件事**：

> `FrozenText` 只是个协议。谁都可以交进来一个"属性访问时才去存储里重算"的对象。
> 装配器看到的仍然只是一段 `text`，**合法得看不出任何异常**。

也就是说：类型形状管住了"调不调构建函数"，管不住"这段字节是不是当场算出来的"。
而后者才是 I3 真正要防的东西 —— F 在边界之间重算，前缀就每轮断裂，缓存全废，
**并且不报错**。

## 判据：常数性

F 的字节来自 `plans` 那一行（P8），所以**装配一行的读取量与内容规模无关**。
于是可以观测：

* 走设计路径的非边界轮，每轮从存储里读的次数是个常数 —— 实测是 **3**：
  `snapshot` × 1（健康探针）+ `read_turns_with_hashes` × 2
  （写盘后读回本轮做 P3 常开断言 + `_foreign_signature` 的 F16 检出读）；
* 一旦有人让 F 在装配期重算（无论藏在属性里还是别处），读次数必然随
  键树规模变化 —— 而"随规模变化"就是可观测的破绽。

这条断言不检查"有没有调 `build_frozen_block`"（那是第一道，静态的），
它检查"这一轮到底摸了几次库"（第三道，行为的）。两者漏的东西不一样。

## 探针必须数**底座**，不能数投影

这是本文件修过一次的缺陷，值得留在原地：

`read_turns` 的实现体只有一行「委托给 `read_turns_with_hashes`」。在投影上挂计数，
等于假设"所有读都从这道门进来" —— 而 `session._foreign_signature`（F16 检出）走的是
**组合入口本身**，它一次都数不到。

实测：改之前探针报「非边界轮 2 次」，真实是 3 次，漏掉的恰恰是**每轮都发生**的
那一次 F16 检出读。于是"非边界轮读次数是常数"这条断言数的是一个**不包含真实
每轮读**的集合 —— 它仍然为真，但**再也不会红**：谁在 `_foreign_signature` 里
加第二次读，断言纹丝不动。

这是检查器退化的第三种形态，前面两种是：被 `continue` 跳过（根本没跑）、
加了 `# noqa`（跑了但豁免）。这第三种最安静：**检查还在跑，但它量的对象换了。**
所以下面除了断言常数，还断言"读过的入口集合"具体是哪两个 —— 只数总数会漏掉
"少了一个、多了一个、总数不变"。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session            # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"

#: 非边界轮每轮应当读几次存储（分解见模块 docstring）。
EXPECTED_READS_PER_TURN = 3

#: 非边界轮允许出现的读入口。与上面的数字是一对：数字抓"多了没多"，
#: 集合抓"换没换"。
EXPECTED_ENTRIES = frozenset({"snapshot", "read_turns_with_hashes"})


class _CountingStore(MemoryStore):
    """按**底座**计数。口径与 `model.bump_store_reads` 保持一致。

    刻意**不** override `read_turns`：它是组合入口的投影，在投影上计数会漏掉直接调
    组合入口的路径（F16 检出就是这么调的）。`read_turn_hashes` 已从端口删除
    （零调用者），所以这里也不需要为它留位置。
    也刻意不数 `begin(write=False)`：`bump_store_reads` 不数它，
    两套口径并存本身就是"两份真相"，而这里要的是和装配侧自检同源的数。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0
        self.by_entry: dict[str, int] = {}

    def _tick(self, entry: str) -> None:
        self.reads += 1
        self.by_entry[entry] = self.by_entry.get(entry, 0) + 1

    def snapshot(self):
        self._tick("snapshot")
        return super().snapshot()

    def read_turns_with_hashes(self, lo, hi):
        self._tick("read_turns_with_hashes")
        return super().read_turns_with_hashes(lo, hi)


def _measure(store: MemoryStore, turns: int = 16,
             interval: int = 4) -> list[tuple[int, int, bool, dict[str, int]]]:
    """跑一段会话，返回 `(轮次, 本轮读次数, 是否边界, 分入口增量)`。"""
    session = Session.open(
        store=store,
        # N=4：让边界稀疏到 4 轮一次，这样有足够多的非边界轮可比
        policy=Policy(compact_interval=interval, verbatim_keep=2),
        system=SYSTEM)
    session.attribute(["T 支付网关", "  P 幂等", "    R 重试上限 3 次"])

    rows: list[tuple[int, int, bool, dict[str, int]]] = []
    for i in range(1, turns + 1):
        before = store.reads
        snap = dict(getattr(store, "by_entry", {}))
        payload = session.send("第 %d 轮：随便说点什么，长度略有变化 %s" % (i, "。" * i))
        after = dict(getattr(store, "by_entry", {}))
        delta = {k: after.get(k, 0) - snap.get(k, 0) for k in set(after) | set(snap)}
        rows.append((i, store.reads - before, payload.boundary,
                     {k: v for k, v in delta.items() if v}))
        session.observe("收到第 %d 轮" % i)

    session.close()
    return rows


class TestReadsAreConstant(unittest.TestCase):
    def test_non_boundary_turns_read_a_constant_number_of_times(self):
        """★ 核心：非边界轮每轮的存储读次数必须相等，且不随轮次增长。"""
        store = _CountingStore()
        rows = _measure(store)

        non_boundary = [(t, d, delta) for (t, d, b, delta) in rows if not b and t >= 2]
        self.assertGreaterEqual(len(non_boundary), 6,
                                "非边界轮太少，这条断言会空过 —— 调小 compact_interval 重跑")

        counts = {d for (_t, d, _delta) in non_boundary}
        self.assertEqual(
            len(counts), 1,
            "非边界轮的存储读次数不一致：%s。"
            "F 的字节应当来自 plans 那一行（P8），每轮读取量必须与内容规模无关；"
            "一旦随轮次变化，说明有人在装配期重建 F（I3）。"
            % sorted(counts))

        per_turn = counts.pop()
        self.assertEqual(
            per_turn, EXPECTED_READS_PER_TURN,
            "非边界轮每轮读了 %d 次存储，设计上是 %d 次（%s）。"
            "多出来的读很可能是有人在装配期取数；少了的读更危险 —— "
            "说明探针口径没跟上端口（在投影上计数会漏掉组合入口的调用）"
            % (per_turn, EXPECTED_READS_PER_TURN, sorted(EXPECTED_ENTRIES)))

        # 只数总数不够：「少了一个、多了一个、总数不变」会被漏掉。
        for (turn, _d, delta) in non_boundary:
            self.assertEqual(frozenset(delta), EXPECTED_ENTRIES,
                             "第 %d 轮读过的入口是 %s，期望 %s："
                             "入口集合变了，说明每轮的读取路径换了地方"
                             % (turn, sorted(delta), sorted(EXPECTED_ENTRIES)))

    def test_f16_detection_read_is_inside_the_measured_set(self):
        """正控：探针必须**看得见** F16 检出那一次读。

        这条单独存在，因为它是上面那条断言最容易退化的地方：把探针从组合入口
        挪回 `read_turns`，其余用例全绿，只有这一条会红。

        第 1 轮是冷启动边界，库里还没有任何轮次，检出读**无从发生**（实测那一轮
        只有两次 `snapshot`），所以从第 2 轮起算。
        """
        store = _CountingStore()
        rows = _measure(store, turns=8)

        for (turn, _d, _b, delta) in rows:
            if turn == 1:
                continue
            self.assertIn(
                "read_turns_with_hashes", delta,
                "第 %d 轮的读数分解里没有组合入口（%s）。"
                "探针很可能又退回到只数 `read_turns` 投影了 —— "
                "那正是这条用例要拦的退化" % (turn, sorted(delta)))

        total = sum(delta.get("read_turns_with_hashes", 0) for (_t, _d, _b, delta) in rows)
        self.assertGreaterEqual(
            total, 2 * (len(rows) - 1),
            "8 轮里 `read_turns_with_hashes` 只被探针看到 %d 次，"
            "少于「第 2 轮起每轮 2 次」" % total)

    def test_boundary_turns_read_more_than_non_boundary(self):
        """反向确认：边界轮**应该**读得更多（它要取快照交给压缩器）。

        没有这一条，上面那条在"每轮都读 0 次"的实现里也会通过 ——
        那就变成了一个永远为真的断言。
        """
        store = _CountingStore()
        rows = _measure(store, turns=9, interval=3)

        boundary_reads = [d for (_t, d, b, _x) in rows if b]
        plain_reads = [d for (_t, d, b, _x) in rows if not b]

        self.assertTrue(boundary_reads, "这一段里没有边界轮，测不到对照")
        self.assertTrue(plain_reads, "这一段里没有非边界轮，测不到对照")
        # 用 any 而不是 max>max：第 1 轮是冷启动边界，它读得并不比普通轮多
        # （`turn_hi == 0` 时探针连 read_turns 都不用调）。要求"每一个边界轮都更多"
        # 是在给冷启动轮加一条它不该承担的义务。
        self.assertTrue(
            any(b > max(plain_reads) for b in boundary_reads),
            "没有任何一个边界轮读得比非边界轮多（边界 %s / 非边界 %s）—— "
            "说明计数没在起作用，上面那条常数断言等于空过"
            % (boundary_reads, plain_reads))

    def test_a_varying_implementation_actually_turns_the_assertion_red(self):
        """正控：常数断言本身必须有区分力（否则它只是"永远为真"）。

        造一个**故意不规则读**的存储：库里有轮次之后，每三轮在 `snapshot()` 之后
        多摸一次库。它必须让「非边界轮读数只有一种取值」这条判据失败 ——
        否则上面那条核心断言测不到任何东西。

        第一版把多余读写成 `read_turns_with_hashes(1, 1)` 无条件调用，结果整段
        读数为 0：第 1 轮库里还没有轮次，那次读抛 `StoreCorruptError` → F1 →
        会话直接降级成 buffer_only，**此后一次库都不读**。正控自己撞出了一个
        真事实（读一个不存在的区间会让会话永久降级），但它没能证明"断言有区分力"。
        所以这里先看 `turn_hi`，只在区间合法时才多读。
        """
        class _Leaky(_CountingStore):
            """库里有轮次后，每三轮多摸一次。"""

            def snapshot(self):
                snap = super().snapshot()
                if snap.turn_hi >= 1 and snap.turn_hi % 3 == 0:
                    self.read_turns_with_hashes(1, 1)      # 多余的读（区间合法）
                return snap

        store = _Leaky()
        rows = _measure(store, turns=12)
        non_boundary = [d for (t, d, b, _x) in rows if not b and t >= 2]
        self.assertTrue(non_boundary, "这一段里没有非边界轮")
        self.assertGreater(
            len(set(non_boundary)), 1,
            "不规则读的存储也被判成常数了（%s）—— "
            "说明常数断言没有区分力，核心用例等于空过" % sorted(set(non_boundary)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
