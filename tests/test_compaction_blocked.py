# -*- coding: utf-8 -*-
"""压缩器算错（``InvariantError``）的处置链：**只停压缩**，不停读、不停写。

## 为什么这条要单独钉

``plans`` 约束违反 / 同号不同字节这两条，原先译 ``StoreCorruptError``（F1），
语义上指向"库坏了"。可库明明是好的，坏的是压缩器 —— 一次 ``r_first_turn`` 算错
并不代表 ``PRAGMA integrity_check`` 会红（它一定全绿），也不代表历史读不出来。

## 三个状态位，三句不同的处置

| 状态位 | 含义 | 处置 |
|---|---|---|
| ``_readable=False`` | 库读不出来 | 此后只用进程内缓冲（A 臂行为） |
| ``_writes_halted=True`` | P9 被违反（两个驱动） | 彻底停写，**不可逆** |
| ``_compaction_blocked`` | 压缩器坏了 | **只**停压缩；读写都还在 |

把第三件事塞进前两个之一，代价分别是"会话再也读不出历史"和"把自己的 bug 说成
调用方的进程模型"。所以这里逐条断言三者互不串味。

## 为什么必须锁存（而不是记一条事件就继续）

同一个压缩器下次还算错。不锁存的话它每 N 轮刷一条事件，把 1024 条环形缓冲刷满，
根因被自己的重复事件挤出去 —— 比不记更坏。所以断言的动作是
「``put_plan`` 的尝试次数**不再增长**」，不是"日志里有记录"。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice import MemoryStore, Policy, Session              # noqa: E402
from ctxlattice.errors import InvariantError                     # noqa: E402
from ctxlattice.policy import DegradeReason                     # noqa: E402

SYSTEM = "你是后端架构师。\n只输出结论。\n"


class _TxnProxy:
    """把 ``put_plan`` 换成本进程的实现缺陷，其余照旧。"""

    def __init__(self, txn, store: "_BrokenCompactorStore") -> None:
        object.__setattr__(self, "_txn", txn)
        object.__setattr__(self, "_store", store)

    def __getattr__(self, name: str):
        return getattr(self._txn, name)

    def __enter__(self) -> "_TxnProxy":
        self._txn.__enter__()
        return self

    def __exit__(self, *exc) -> object:
        return self._txn.__exit__(*exc)

    def put_plan(self, plan, **kwargs) -> None:
        self._store.plan_attempts += 1
        raise InvariantError(
            "压缩产出违反 plans 表约束（实现缺陷）：r_first_turn = %d 超过 "
            "epoch_turn + 1" % (plan.epoch_turn + 5))


class _BrokenCompactorStore(MemoryStore):
    """写事务里的 ``put_plan`` 永远抛 ``InvariantError``；其余读写都正常。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.plan_attempts = 0
        self.write_attempts = 0

    def begin(self, *, write: bool = False):
        if write:
            self.write_attempts += 1
        txn = super().begin(write=write)
        return _TxnProxy(txn, self) if write else txn


class TestInvariantErrorOnlyStopsCompaction(unittest.TestCase):
    def _session(self) -> tuple[Session, _BrokenCompactorStore]:
        store = _BrokenCompactorStore()
        session = Session.open(store=store,
                               policy=Policy(compact_interval=1, verbatim_keep=1),
                               system=SYSTEM)
        session.attribute(["T 议题", "  P 子目的", "    R 一条要求"])
        return session, store

    def test_store_stays_writable(self):
        """★ 判据一：库是好的 → ``store_writable`` 必须还是 True。"""
        session, store = self._session()
        session.send("第 1 轮")
        self.assertTrue(session.stats().store_writable,
                        "压缩器算错被当成了'库坏了'：读侧被一起关掉了")
        session.close()

    def test_payload_is_marked_internal(self):
        """★ 判据二：报告要看得见 —— 二元组，且原因不是 ``store_corrupt``。"""
        session, store = self._session()
        session.send("第 1 轮")
        payload = session.send("第 2 轮")
        self.assertEqual((payload.degraded, payload.degrade_reason),
                         (True, DegradeReason.INTERNAL.value),
                         "压缩永久停摆，payload 却报成正常（或原因指向了别处）")
        session.close()

    def test_compaction_is_never_retried(self):
        """★ 判据三：锁存 —— 此后一次压缩都不许再试。

        只记一条事件就继续的实现会在这里红：它会每 N 轮撞一次，
        把环形缓冲刷满，根因被自己的重复事件挤出去。
        """
        session, store = self._session()
        session.send("第 1 轮")
        self.assertEqual(store.plan_attempts, 1, "第一轮没走到压缩，用例前提不成立")
        for i in range(2, 8):
            session.send("第 %d 轮" % i)
            session.observe("收到")
        self.assertEqual(store.plan_attempts, 1,
                         "压缩器已判为缺陷，却又被重试了 %d 次"
                         % (store.plan_attempts - 1))
        session.close()

    def test_writes_are_not_halted(self):
        """★ 判据四：不许串味成 F16 —— 那会把可修的缺陷变成不可逆停写。

        观测口刻意用**数据**（库里第几轮），不用内部字段：``_writes_halted`` 改个名
        这条用例就假绿，而"turn 到底落没落盘"改不了名。

        ⚠️ 别用 ``store.rev`` 当"库被写过"的探针：``rev`` 只在**键树内容指纹**变化时
        +1（P4），写轮次不会动它。实测这段里 ``rev`` 恒为 1，而四轮 turn 全部落库 ——
        一个恒等的观测量会让"停写"和"没停写"看起来一模一样，比不观测更坏。
        """
        session, store = self._session()
        session.send("第 1 轮")
        self.assertFalse(session._writes_halted,
                         "压缩器算错被说成了'两个驱动在抢'（不可逆停写）")
        before_turn = store.snapshot().turn_hi
        before_tx = store.write_attempts
        for i in range(2, 5):
            session.send("第 %d 轮" % i)
            session.observe("收到")
        self.assertEqual(store.snapshot().turn_hi, 4,
                         "压缩失败之后轮次没再落盘：%d → %d"
                         % (before_turn, store.snapshot().turn_hi))
        # `>= 2` 而不是 `> 0`：写一次只等于"这一轮写过"，而这一条要断的是
        # **失败之后仍然在写** —— 一个只写一次然后永久停住的实现也能让 `> 0` 绿。
        self.assertGreaterEqual(
            store.write_attempts - before_tx, 2,
            "压缩失败之后只发起了 %d 次写事务 —— 不够证明写入路径还活着"
            % (store.write_attempts - before_tx))
        session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
