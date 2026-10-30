# -*- coding: utf-8 -*-
"""fail-open 的边界：``send()`` / ``observe()`` 永不抛；``open()`` / ``attribute()`` 按签名抛。

这是"能不能上线"的分界线，不是"体验好不好"的问题：

* 装配路径上的任何故障都必须退化成**可发的 payload**（F12 → F13），
  否则一次磁盘抖动就是一次用户可见的 500；
* 但配置错误必须**大声失败**（A4）—— 静默降级会让"参数配错了"伪装成"运行正常"，
  而它在 20 轮后才以"成本高了一倍"的形式出现。

另一半同样重要：**只捕 ``Exception``**。``KeyboardInterrupt`` / ``SystemExit``
必须穿透，否则 Ctrl-C 会被吞进降级路径，进程退不出去。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session  # noqa: E402
from ctxlattice.errors import (  # noqa: E402
    AttributionError,
    PolicyError,
    StoreCorruptError,
    StoreIOError,
)

SYSTEM = "你是后端架构师。\n只输出结论。\n"


class _BrokenSnapshotStore(MemoryStore):
    """打开之后快照必炸（F1：存储损坏）。

    注意它**必须先能打开**：F2 说得很清楚 —— 第一次打开就坏，构造期要大声失败。
    本类模拟的是"跑到第 3 轮时盘坏了"。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.broken = False

    def snapshot(self):
        if self.broken:
            raise StoreCorruptError("第 7 行：头部计数不符（声称 3 个 T，实测 2 个）")
        return super().snapshot()


class _UnwritableStore(MemoryStore):
    """写必炸（F3：磁盘满）。"""

    def begin(self, *, write: bool = False):
        if write:
            raise StoreIOError("磁盘已满（模拟）", errno=28)
        return super().begin(write=write)


class TestFailOpen(unittest.TestCase):
    def test_send_survives_broken_snapshot(self):
        store = _BrokenSnapshotStore()
        session = Session.open(store=store,
                              policy=Policy(compact_interval=2, verbatim_keep=1),
                              system=SYSTEM)
        session.send("先正常一轮，把会话推进到可压缩状态")
        session.observe("收到。")            # 不 observe 会先报 missing_assistant
        store.broken = True
        payload = session.send("存储坏了也要能发。")

        self.assertTrue(payload.degraded)
        self.assertEqual(payload.degrade_reason, "store_corrupt")
        self.assertTrue(payload.to_bytes(), "降级 payload 不能是空字节")
        self.assertIn("存储坏了也要能发", payload.text)
        self.assertGreaterEqual(session.stats().degradations, 1)
        session.close()

    def test_send_survives_unwritable_store(self):
        store = _UnwritableStore()
        session = Session.open(store=store, policy=Policy(), system=SYSTEM)
        payload = session.send("写不了也要能发。")

        # §10.2「写盘是旁路」管的是**正文**：payload 在写盘之前就已确定，失败也不回滚它
        # （下面 ``assertIn`` 还在）。元数据不在此列 —— 写失败是这一轮确凿发生的事，
        # 必须当轮可见、且必须能归因：``store_writable`` 是弱面，F1/F3/F16/压缩器缺陷
        # 全都让它变 False，运维看到它只会去查磁盘。
        self.assertTrue(payload.degraded,
                        "写失败那一轮没标降级：调用方拿到的是一份全绿的 payload")
        self.assertEqual(payload.degrade_reason, "disk_full",
                         "降级为真、原因错 —— 只有这个字段能拆穿。"
                         "这里的值必须是 disk_full，不是 store_io：``_UnwritableStore`` "
                         "抛的是 ``errno=28``（ENOSPC），而 ``classify()`` 先判 errno "
                         "再判类型 —— 「磁盘满」和「读写出错」的处置不同，"
                         "报告里混成一个值就等于把去腾磁盘的那条线索删了")
        self.assertIn("写不了也要能发", payload.text)
        self.assertFalse(session.stats().store_writable)
        session.close()

    def test_send_survives_store_that_raises_on_every_method(self):
        class Hostile(MemoryStore):
            hostile = False

            def __getattribute__(self, name):
                if (name in ("snapshot", "read_turns", "begin")
                        and object.__getattribute__(self, "hostile")):
                    raise RuntimeError("端口实现炸了：%s" % name)
                return object.__getattribute__(self, name)

        store = Hostile()
        session = Session.open(store=store, policy=Policy(), system=SYSTEM)
        session.send("先正常一轮")
        object.__setattr__(store, "hostile", True)
        payload = session.send("任何内部异常都不能传出去。")
        self.assertTrue(payload.to_bytes())
        self.assertTrue(payload.degraded)
        session.close()

    def test_keyboard_interrupt_propagates(self):
        """**只捕 Exception**：Ctrl-C 必须能穿透，否则进程退不出去。"""
        session = Session.open(store=MemoryStore(store_id="kb"), policy=Policy(),
                               system=SYSTEM)

        def boom(*args, **kwargs):
            raise KeyboardInterrupt()

        session._decide_boundary = boom           # type: ignore[assignment]
        with self.assertRaises(KeyboardInterrupt):
            session.send("这一轮应当把 KeyboardInterrupt 抛出来")

    def test_system_exit_propagates(self):
        session = Session.open(store=MemoryStore(store_id="se"), policy=Policy(),
                               system=SYSTEM)

        def boom(*args, **kwargs):
            raise SystemExit(3)

        session._decide_boundary = boom           # type: ignore[assignment]
        with self.assertRaises(SystemExit):
            session.send("这一轮应当把 SystemExit 抛出来")

    def test_observe_never_raises(self):
        session = Session.open(store=MemoryStore(store_id="ob"), policy=Policy(),
                               system=SYSTEM)
        session.observe("还没 send 就来 observe")   # 只记降级，不抛
        payload = session.send("正常一轮")
        self.assertTrue(payload.to_bytes())
        session.close()

    def test_attribute_raises_on_bad_delta(self):
        """调用方给错增量 = 程序错误，必须可见（不是环境故障）。"""
        session = Session.open(store=MemoryStore(store_id="at"), policy=Policy(),
                               system=SYSTEM)
        session.send("第一轮")
        with self.assertRaises(AttributionError):
            session.attribute(["P 没有父事物的目的"])
        with self.assertRaises(AttributionError):
            session.attribute(["T "])          # 空名称
        session.close()

    def test_attribute_swallows_store_failure(self):
        """存储失败不抛（返回值 None），但非法增量要抛 —— 两者不能混。"""
        session = Session.open(store=_UnwritableStore(), policy=Policy(),
                               system=SYSTEM)
        session.send("第一轮")
        self.assertIsNone(session.attribute(["T 品牌定位"]))
        session.close()

    def test_open_raises_on_bad_policy(self):
        with self.assertRaises(PolicyError):
            Session.open(store=MemoryStore(store_id="po"),
                         policy=Policy(compact_interval=2, verbatim_keep=5),
                         system=SYSTEM)
        with self.assertRaises(PolicyError):
            Session.open(store=MemoryStore(store_id="po"),
                         policy=Policy(high_water=1.5), system=SYSTEM)

    def test_open_raises_when_store_cannot_open(self):
        """F2：库**打不开**（文件损坏 / 权限 / schema 版本过高）→ 构造期大声失败。

        与 F1 的分界：F1 是"打得开、但内容有洞"，那条必须降级而不是抛
        （检测点在 ``read_turns`` / ``snapshot``，补救是"仅用缓冲装配"）。
        两者搞混的后果是双向的：该降级的地方变成构造就炸，该炸的地方变成
        静默服务一个坏库。
        """

        class _CantOpen(MemoryStore):
            def open(self):
                raise StoreCorruptError("库文件不是 SQLite 格式（F2）")

        with self.assertRaises(StoreCorruptError):
            Session.open(store=_CantOpen(store_id="f2"), system=SYSTEM)

    def test_open_tolerates_content_damage(self):
        """F1：内容有洞 → 打开成功、已标记不可写，且第一轮就能发出降级 payload。"""
        store = _BrokenSnapshotStore(store_id="f1")
        store.broken = True
        session = Session.open(store=store, system=SYSTEM)
        self.assertFalse(session.stats().store_writable)
        payload = session.send("坏库也要能发")
        self.assertTrue(payload.degraded)
        session.close()

    def test_close_is_idempotent_and_never_raises(self):
        session = Session.open(store=MemoryStore(store_id="cl"), policy=Policy(),
                               system=SYSTEM)
        session.send("一轮")
        session.close()
        session.close()
        payload = session.send("关闭之后仍然不抛（内部异常 → 兜底 payload）")
        self.assertTrue(payload.to_bytes())


class TestErrnoDecidesTheCodeNotTheTypeName(unittest.TestCase):
    """★ **异常的类型名不等于降级码** —— 中间隔着一个 ``classify()``。

    这条用例的由来是一次报告事故：说到 ``_UnwritableStore`` 那一轮的 reason 时，
    凭"``StoreIOError`` → ``store_io``"这个名字直觉写成了 ``store_io``，
    而它抛的是 ``errno=28``（ENOSPC），``classify()`` **先判 errno 再判类型**，
    实际给的是 ``disk_full``。

    这个错误的形状值得单独记：**我说出去的值，和我文件里实际的值，在报告里长得一模一样。**
    所以它不能靠"读一遍报告"发现，只能靠**读回 ``classify()``**。

    而两个值的运维含义不同：``disk_full`` 说"去腾磁盘"，``store_io`` 说"读写出错了"。
    **混成一个值 = 把"腾磁盘"那条线索删掉** —— 所以两个方向都要钉，不是只钉命中过的那个。
    """

    def test_disk_errnos_map_to_disk_full(self):
        import errno as _errno

        from ctxlattice.degrade import classify
        from ctxlattice.policy import DegradeReason

        for code in (_errno.ENOSPC, _errno.EACCES, _errno.EPERM, _errno.EROFS):
            got, _detail = classify(StoreIOError("磁盘满（模拟）", errno=code))
            self.assertIs(got, DegradeReason.DISK_FULL,
                          "errno=%d 归成了 %r：这条 errno 是「腾磁盘」的证据，"
                          "不该被说成读写出错" % (code, got))

    def test_other_errnos_map_to_store_io(self):
        import errno as _errno

        from ctxlattice.degrade import classify
        from ctxlattice.policy import DegradeReason

        for code in (None, _errno.EIO):
            got, _detail = classify(StoreIOError("读写出错（模拟）", errno=code))
            self.assertIs(got, DegradeReason.STORE_IO,
                          "errno=%r 归成了 %r：没有磁盘证据的 IO 失败不许冒充 disk_full"
                          % (code, got))

    def test_the_two_directions_stay_distinct(self):
        """反方向的分量：两个码**必须不同**。

        只写上面两条、不写这一条，一个"所有 ``StoreIOError`` 都返回同一个码"的实现
        会红——但如果哪天有人把两条断言一起改宽（比如都改成 assertIsNotNone），
        就没有任何东西守着"它们其实是两件事"。
        """
        import errno as _errno

        from ctxlattice.degrade import classify

        disk, _ = classify(StoreIOError("x", errno=_errno.ENOSPC))
        io, _ = classify(StoreIOError("x", errno=_errno.EIO))
        self.assertNotEqual(disk, io)


if __name__ == "__main__":
    unittest.main(verbosity=2)
