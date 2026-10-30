# -*- coding: utf-8 -*-
"""正控：**单驱动撞不到 `put_turn` 的同号不同内容。**

## 为什么这条必须在套件里，而不是留在 scratch

`put_turn` 的分叉检测现在是 F16 的**写侧出口**，而它按 §10.6 是**单次即停写**——
这个判决的全部前提是"单驱动撞不到它"。前提若假，后果不是"少报一次"，
是**正常会话被自己永久停写**（最贵的一类误伤）。前提留在脚本里，等于把一条规则
建在一段没人会再跑的东西上。

## 这条用例证什么、不证什么

* **证**：`send` + 两次 `observe` + 周期翻转 `pinned` 的合法用法，在两种实现上都不产生
  任何 `concurrent_driver` / `store_corrupt` / `internal` 事件，且 `_writes_halted` 恒 False。
* **不证**："分叉检测本身有效"。那是另一条用例的事（注入第二个驱动）。

## 负控必须自证"控制条件成立"

一条"冲突 0 条"的断言，在**比较分支根本没跑过**时同样会绿——那正是本仓库反复踩的
"恒等于正常的指标"。所以下面除了断言没有冲突，还断言**同一轮被写过不止一次**
（`write_attempts > 轮数`）：`observe()` 会把该轮连同新回复再落一次库，于是
`put_turn` 必然走进"part 已存在 → 比 hash"那条分支。若哪天实现改成只写增量，
这个断言会先红——它红的意思是**控制条件没了，别把绿当成结论**。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session            # noqa: E402
from ctxlattice.policy import DegradeReason                    # noqa: E402
from ctxlattice.store_sqlite import SqliteStore                # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"
TURNS = 40

#: 这几类原因一旦出现，说明"单驱动不可达"这个前提破了。
FORBIDDEN = frozenset({
    DegradeReason.CONCURRENT_DRIVER,
    DegradeReason.STORE_CORRUPT,
    DegradeReason.INTERNAL,
})


class _CountingStore(MemoryStore):
    """只数写事务次数，其余行为原样。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)


class _CountingSqliteStore(SqliteStore):
    """同一口径的 SQLite 版：不数写事务，负控就只在内存实现上成立。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        return super().begin(write=write)


def _drive(session: Session, store) -> list[tuple[int, str]]:
    """跑一段"合法但花样齐全"的会话：send + 两次 observe + 周期翻转 pinned。"""
    session.attribute(["T 支付网关", "  P 幂等", "    R 重试上限 3 次"])
    bad: list[tuple[int, str]] = []

    for i in range(1, TURNS + 1):
        payload = session.send("第 %d 轮：内容略有变化 %s" % (i, "。" * (i % 7)))
        if payload.degrade_reason is not None:
            bad.append((i, payload.degrade_reason))

        session.observe("第 %d 轮回复" % i)
        # 同一轮再 observe 一次：回复应当**追加**（part 1 hash 相同、part 2 是新增），
        # 不该被当成"同号不同内容"。
        session.observe("第 %d 轮补充回复" % i)

        # 每 5 轮翻转 pinned：走 `UPDATE turn SET pinned`，绕开 hash 比较。
        if i % 5 == 0:
            session.attribute(["T 支付网关", "  P 幂等", "    R 重试上限 3 次 [钉住]"])
        elif i % 5 == 2:
            session.attribute(["T 支付网关", "  P 幂等", "    R 重试上限 3 次"])
    return bad


class TestSingleDriverNeverForks(unittest.TestCase):
    """★ 前提用例：`put_turn` 的同号不同内容，在单驱动下不可达。"""

    def _assert_clean(self, session: Session, store, label: str) -> None:
        events = [e for e in session.degradation_log()
                  if getattr(e.reason, "value", e.reason) in
                  {r.value for r in FORBIDDEN}]
        self.assertEqual(
            events, [],
            "%s：单驱动产生了 %d 条禁止的降级事件，第一条是 %r。"
            "「写侧出口单次即停写」的前提破了 —— 正常会话会被自己打死"
            % (label, len(events),
               getattr(events[0].reason, "value", None) if events else None))
        self.assertFalse(session._writes_halted,
                         "%s：单驱动被停写了" % label)
        self.assertTrue(session.stats().store_writable,
                        "%s：单驱动把库标成不可写" % label)

    def _assert_control_condition(self, store, label: str) -> None:
        """负控自证：同一轮确实被写过不止一次，比较分支真的跑过。"""
        self.assertGreater(
            store.write_attempts, TURNS,
            "%s：写事务 %d 次 ≤ 轮数 %d，说明同一轮从未被重写 —— "
            "`put_turn` 的'part 已存在→比 hash'分支根本没跑过，"
            "这条用例此时绿得毫无意义" % (label, store.write_attempts, TURNS))

    def test_memory_store(self):
        store = _CountingStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=12, verbatim_keep=6),
                               system=SYSTEM)
        try:
            self.assertEqual(_drive(session, store), [], "出现了降级轮")
            self._assert_clean(session, store, "MemoryStore")
            self._assert_control_condition(store, "MemoryStore")
        finally:
            session.close()

    def test_sqlite_store(self):
        tmp = tempfile.mkdtemp(prefix="ctxlattice-1drv-")
        try:
            path = os.path.join(tmp, "s.db")
            store = _CountingSqliteStore(path, session_id="s-1drv")
            session = Session.open(store=store,
                                   policy=Policy(compact_interval=12, verbatim_keep=6),
                                   system=SYSTEM)
            try:
                self.assertEqual(_drive(session, store), [], "出现了降级轮")
                self._assert_clean(session, store, "SqliteStore")
                self._assert_control_condition(store, "SqliteStore")
            finally:
                session.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
