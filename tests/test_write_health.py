# -*- coding: utf-8 -*-
"""写路径的**状态位**：三种失败、三个处置，一条事件都不许「只记不改」。

## 这一批用例要钉的那个失败形状

判据（团队定案）：

    **事件是流水，状态是快照；任何一条事件，要么改一个状态位，要么它就不该被写。**

原来的 ``_persist_turn`` 只对 ``SessionConflictError`` / ``StoreIOError`` 动状态位，
其余（``StoreCorruptError`` / ``InvariantError`` / 任何异常）只 ``_record()`` 一条事件。
后果不是「少记一笔」，而是**降级为真、状态为假**：

1. 第 N 轮写盘抛 ``StoreCorruptError`` → 事件里有 ``store_corrupt``，``_writable`` 仍是 True；
2. 第 N+1 轮探针 ``snapshot()`` 成功 → ``_clear_degraded_mode_if_healthy`` 三元合取成立
   （``_readable`` 一直 True、``_writable`` 没被动过、``_compaction_blocked`` 是 None）
   → 把 ``_degraded_mode`` 清掉；
3. 于是**每一轮** payload 的 ``degraded`` 都是 False —— 一个看起来全绿的会话，
   而它的库从第 N 轮起再没写进去过任何东西。

这个形状比「完全没记」更难发现：``degradation_log()`` 里确实有一条记录，
任何「有没有降级事件」的断言都通过；只有 ``stats().store_writable`` 与后续轮的
``payload.degraded`` 能拆穿它。

## 三个状态位各有唯一含义（不许互相借用）

| 位 | 含义 | 谁置它 | 用户可见输出 |
|---|---|---|---|
| ``_readable=False`` | 库**读**不出来（F1/F3） | 探针 / ``_seed`` / 边界快照失败 | ``store_writable`` + ``degraded`` |
| ``_writable=False`` | **写**不进去（本文件的主题） | 三处写入口 | ``store_writable`` + ``degraded`` |
| ``_compaction_blocked`` | 压缩器坏了（实现缺陷） | ``_try_boundary`` / 探针 | ``degraded``（每轮） |

所以本文件里有一条**负向断言**同样重要：``InvariantError`` 在读侧不许把会话打成
「读不出来」（那会把一个好库上的会话降级成只有缓冲），在写侧则必须落到写健康位。
**同一个异常码，两处处置不同 —— 因为它们回答的问题不同。**
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session, SqliteStore   # noqa: E402
from ctxlattice.errors import (                                    # noqa: E402
    InvariantError,
    SessionConflictError,
    StoreCorruptError,
    StoreError,
)
from ctxlattice.model import BlockKind                             # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"

#: 写路径用例统一用它：``compact_interval`` 调到很大，让「压缩」不出现在射程里
#: （压缩有自己的写事务，会把 ``write_attempts`` 的读数搅浑）。
NO_COMPACT = Policy(compact_interval=1000, verbatim_keep=1)


def _writes(store) -> int:
    return store.write_attempts


class _BrokenWriteStore(MemoryStore):
    """**每一次**写事务都抛指定异常；读侧完全健康（``snapshot`` 照常成功）。

    「读侧健康」是这条用例组的全部力量所在：旧实现之所以能把状态位漏掉，正是
    因为下一次读成功会替它把标记清掉。读侧一旦也坏，``_readable=False`` 兜住，
    用例就测不到写健康位了。
    """

    def __init__(self, factory, **kwargs) -> None:
        super().__init__(**kwargs)
        self._factory = factory
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
            raise self._factory()
        return super().begin(write=write)


class TestWriteFailureLandsOnTheWriteHealthBit(unittest.TestCase):
    """三种写失败 → 同一个状态位，三种原因。"""

    def _run(self, factory, expected_reason: str):
        store = _BrokenWriteStore(factory, store_id="m", session_id="s")
        session = Session.open(store=store, policy=NO_COMPACT, system=SYSTEM)

        first = session.send("第一轮")
        # §10.2「写盘是旁路」管的是**正文**：已装配的 payload 不回滚，一个字节都不动。
        # 元数据不在此列 —— 写失败是这一轮**确凿发生**的事，必须当轮可见，且必须能归因。
        #
        # 曾经这里写的是 ``assertFalse(first.degraded)``，注释是「payload 在写盘之前就
        # 定稿了 → 这一轮不标」。那句话把**正文不回滚**读成了**元数据不标**：
        # 同一函数里 F4 就在做同款事后 replace（见 ``Session.send`` 里 F4 之后那段）。
        self.assertTrue(first.degraded,
                        "写失败那一轮没标降级：调用方拿到的是一份全绿的 payload，"
                        "而库从这一轮起就没写进去过")
        self.assertEqual(first.degrade_reason, expected_reason,
                         "降级为真、原因错 —— 这是唯一一种所有断言都通过、"
                         "只有这个字段能拆穿的失败")

        self.assertFalse(session.stats().store_writable,
                         "写失败之后 store_writable 仍是 True：状态位没动过 —— "
                         "那么下一轮读成功就会把降级标记清掉")
        self.assertEqual(_writes(store), 1,
                         "写失败之后仍在发起写事务（%d 次）：判据是「不再发起写事务」，"
                         "不是「不再抛异常」" % _writes(store))

        second = session.send("第二轮")
        self.assertTrue(second.degraded,
                        "第 2 轮 degraded=False，但库从第 1 轮起就没写进去过 —— "
                        "这正是「事件是流水、状态是快照」那条判据要堵的形状")
        self.assertEqual(second.degrade_reason, expected_reason)

        # 往下再走两轮：状态位既不许自己恢复，也不许重新开始写。
        session.send("第三轮")
        session.send("第四轮")
        self.assertEqual(_writes(store), 1, "会话又开始写盘了（状态位被读成功重置）")
        self.assertEqual(session.send("第五轮").degrade_reason, expected_reason)

    def test_store_corrupt_on_write_is_not_just_an_event(self):
        """★ 顾图南实测到的那一条：``put_turn`` 抛 ``StoreCorruptError`` 时状态位是假的。"""
        self._run(lambda: StoreCorruptError("轮次表被外部改过"), "store_corrupt")

    def test_invariant_on_write_is_not_just_an_event(self):
        """不只 ``StoreIOError``：``InvariantError`` 在写侧也必须落写健康位。

        注：写侧的 INTERNAL 与读侧**不同处置** —— 读侧只停压缩（库是好的），
        写侧是「这一次写没成，此后不再试」。见文件头那张表。
        """
        self._run(lambda: InvariantError("turn 行与 session.turn_seq 对不上"), "internal")

    def test_plain_store_error_on_write_is_not_just_an_event(self):
        """兜底：连未细分的 ``StoreError`` 也不许「只记一条事件」。"""
        self._run(lambda: StoreError("端口实现没细分的失败"), "store_io")

    def test_attribute_failure_lands_on_the_same_bit(self):
        """``attribute()`` 是 ``_persist_turn`` 的**同形状位置**，同一条判据。

        它的通用分支原先也是"只记一条事件"：``StoreCorruptError`` 之后
        ``store_writable`` 还是 True，下一轮 ``snapshot()`` 成功就把标记清了。
        这一条与上面三条的唯一区别是入口不同 —— 判据不变，代码就该同向。
        """
        store = _BrokenWriteStore(lambda: StoreCorruptError("键树表被外部改过"),
                                  store_id="m", session_id="s")
        session = Session.open(store=store, policy=NO_COMPACT, system=SYSTEM)

        self.assertIsNone(session.attribute(["T 议题", "  P 子目的"]),
                          "存储失败不该从 attribute() 抛出来（A4）")
        self.assertFalse(session.stats().store_writable,
                         "attribute() 写失败没有落到写健康位")

        first = session.send("第一轮")
        self.assertTrue(first.degraded, "标记在装配之前就已锁存，第 1 轮就该标")
        self.assertEqual(first.degrade_reason, "store_corrupt")

    def test_healthy_store_keeps_the_bit_green(self):
        """正控：同一条链路上正常写盘时，状态位必须是 True、payload 不许被标。

        没有这一条，上面那句 ``assertFalse(store_writable)`` 在「状态位永远是 False」
        的实现下也会绿。
        """
        store = MemoryStore(store_id="m", session_id="s")
        session = Session.open(store=store, policy=NO_COMPACT, system=SYSTEM)
        # 先给库一点内容：空库上每一轮都会撞 F9（``empty_store_no_compact``），
        # 那与本题无关，会把正控变成"验 F9 的用例"。
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        for i in range(1, 4):
            payload = session.send("第 %d 轮" % i)
            self.assertFalse(payload.degraded, "正常路径被标成了降级")
            session.observe("收到")     # 不登记回复会触发 missing_assistant，与本题无关
        self.assertTrue(session.stats().store_writable)


class _InvariantSnapshotStore(MemoryStore):
    """``snapshot()`` 抛 ``InvariantError``（模拟存储实现/压缩器算错不变量）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.break_snapshot = False
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        if self.break_snapshot:
            raise InvariantError("快照自检失败（模拟实现缺陷，库本身是好的）")
        return super().snapshot()


class TestProbeInvariantBlocksCompactionNotReads(unittest.TestCase):
    """读侧的 ``InvariantError``：**只停压缩**，不许把会话降成「读不出来」。

    三个断言各挡一种实现错误：

    1. ``store_writable`` 仍 True —— 挡住「把 INTERNAL 当 F1 处理」（库是好的）；
    2. payload 仍带 F —— 挡住「顺手把 plan/F 丢了」（那会让下一轮前缀断，I1 破，
       而且 ``degraded`` 已经 True，前缀断言按设计会跳过，**没有任何东西会红**）；
    3. 下一轮原因仍是 ``internal`` —— 钉住 ``_clear_degraded_mode_if_healthy``
       的第三合取项（``_compaction_blocked is None``）。少一项，这里下一轮就变回绿。
    """

    def test_invariant_during_the_probe_does_not_kill_the_read_side(self):
        store = _InvariantSnapshotStore(store_id="m", session_id="s")
        session = Session.open(store=store, policy=NO_COMPACT, system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        session.send("第一轮")
        session.observe("收到")
        self.assertIsNotNone(session.force_compact(), "这一段没压出 plan，下面的断言就空了")

        store.break_snapshot = True
        payload = session.send("第二轮")

        self.assertTrue(payload.degraded)
        self.assertEqual(payload.degrade_reason, "internal")
        self.assertTrue(session.stats().store_writable,
                        "读侧的不变量失败被当成了「库读不出来」：库明明能读，"
                        "代价却是这个会话从此不用历史")
        self.assertTrue(any(b.kind is BlockKind.F for b in payload.blocks),
                        "plan/F 被丢掉了：下一轮 payload 会少一整块，前缀断在没人看的地方")

        # ★ 让库**恢复健康**、再让一次写盘成功，然后才看下一轮。
        #
        # 两步都不能省：``_clear_degraded_mode_if_healthy`` 只在"某次操作成功了"的
        # 地方被调用（``_seed`` / ``attribute()`` 成功 / 边界快照成功）。少了成功那一步，
        # 两种实现都不会去清标记，断言就测不到"第三合取项有没有挡住清除"这件事。
        store.break_snapshot = False
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求 [钉住]"])
        nxt = session.send("第三轮")
        self.assertTrue(nxt.degraded,
                        "库已经恢复健康，但压缩位没锁存 —— 会话从此不再压缩却报全绿："
                        "「一个状态位如果没有任何一条用户可见的输出依赖它，"
                        "它就不是状态，是注释」")
        self.assertEqual(nxt.degrade_reason, "internal",
                         "第 3 轮原因变成 %r：``_clear_degraded_mode_if_healthy`` 的"
                         "第三合取项（``_compaction_blocked is None``）没在挡"
                         % (nxt.degrade_reason,))


class _ConflictWriteStore(MemoryStore):
    """``put_turn`` 撞同一轮的字节（模拟另一个驱动已经写过这一轮）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.conflict = False
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
            if self.conflict:
                raise SessionConflictError(
                    "第 2 轮 part=0 的内容与已存记录不同（P9 被违反）")
        return super().begin(write=write)


class TestTurnConflictHaltsOnTheFirstObservation(unittest.TestCase):
    """★ 写侧冲突**单次即停写**，不需要「第二次观察」。

    判据：F16 的二次确认是为**跨快照的内容比对**准备的 —— 那条证据理论上能由读
    时序造成。而 ``put_turn`` 的这一支在**写事务内**、不跨快照：时序造不出它。
    证据强度不同，动作就该不同。所以下面的断言是「第 2 轮就停」，不是「第 3 轮才停」。

    （若实现退回「只登记怀疑」，第 2 轮的 ``degrade_reason`` 会是 None —— 这条用例红。）
    """

    def test_conflict_halts_writes_immediately(self):
        store = _ConflictWriteStore(store_id="m", session_id="s")
        store.conflict = True          # 从第 1 轮起就撞：模拟「库里已经有这一轮了」
        session = Session.open(store=store, policy=NO_COMPACT, system=SYSTEM)

        session.send("第一轮")
        before = _writes(store)
        self.assertFalse(session.stats().store_writable)

        second = session.send("第二轮")
        self.assertTrue(second.degraded, "第 2 轮没有降级：冲突只被记了一条事件")
        self.assertEqual(second.degrade_reason, "concurrent_driver",
                         "冲突被译成了 %r —— 它必须指向调用方（两个驱动），"
                         "不能指向库" % (second.degrade_reason,))

        session.send("第三轮")
        session.send("第四轮")
        self.assertEqual(_writes(store), before,
                         "停写之后仍在发起写事务（%d → %d）：判据是「不再发起写事务」"
                         % (before, _writes(store)))


class _CountingMemory(MemoryStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)


class _CountingSqlite(SqliteStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)


class TestTwoRealDriversAgreeOnBothBackends(unittest.TestCase):
    """★ 条 3 的验收：**同一个场景在两个后端各跑一遍**，行为必须逐点一致。

    场景是货真价实的 P9 违反：两个 ``Session`` 在同一个库上各自从第 1 轮开始写，
    A 先落了第 1 轮，B 再写第 1 轮时字节不同 → 写路径第一次撞击就在这里。

    为什么必须两个后端都跑：这正是本轮暴露出来的问题形态 —— 两个实现在同一件事上
    给出了**相反**的结论（一个静默、一个抛），而两边的用例各自都是绿的。只有把
    同一个场景在两个后端上跑一遍、断言同一组结论，「逐点一致」才有内容。

    断言组（两后端逐字相同）：

    * B 的 ``send`` 不抛（A4：``send`` 永不抛）；
    * B 第 2 轮 ``degraded=True`` 且原因为 ``concurrent_driver`` —— 不是
      ``store_corrupt``（顾图南在真库上实测到的误诊：那颗信号被译成「库坏了」）；
    * B 不再发起写事务，A 照常可写；
    * 库里的第 1 轮**仍是 A 的内容**（B 的那次写没有覆盖任何字节）。
    """

    def _drive(self, store_a, store_b):
        a = Session.open(store=store_a, policy=NO_COMPACT, system=SYSTEM)
        b = Session.open(store=store_b, policy=NO_COMPACT, system=SYSTEM)
        a.send("A 的第一句")
        a.observe("A 的回复")

        b.send("B 的第一句")            # 同一轮号、不同字节 → 写路径冲突
        before = _writes(store_b)

        second = b.send("B 的第二句")
        self.assertTrue(second.degraded)
        self.assertEqual(second.degrade_reason, "concurrent_driver",
                         "真双驱动的第一次撞击被译成了 %r" % (second.degrade_reason,))
        self.assertFalse(b.stats().store_writable)

        b.send("B 的第三句")
        self.assertEqual(_writes(store_b), before,
                         "B 停写之后仍在发起写事务（%d → %d）" % (before, _writes(store_b)))

        self.assertTrue(a.stats().store_writable, "A 被 B 的冲突连坐了")
        a.send("A 的第二句")

        stored = store_a.read_turns(1, 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].user_text, "A 的第一句",
                         "第 1 轮被后到的驱动改写了 —— 静默覆盖会让缓存命中率虚高")

    def test_memory_backend(self):
        shared = _CountingMemory(store_id="m", session_id="s")
        self._drive(shared, shared)

    def test_sqlite_backend(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "store.db")
            a = _CountingSqlite(path, store_id="t", session_id="s1",
                                transcript_hash="b" * 64)
            b = _CountingSqlite(path, store_id="t", session_id="s1",
                                transcript_hash="b" * 64)
            self._drive(a, b)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
