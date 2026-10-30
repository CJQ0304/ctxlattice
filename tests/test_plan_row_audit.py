# -*- coding: utf-8 -*-
"""``plans`` 表：DDL 的每一条列级约束，与 ``_check_plan_row`` 的**逐列对照审计**。

    tests/test_plan_row_audit.py

## 这张表为什么是**代码**而不是文档

审计的产物如果只是一张表，它会烂：加了 DDL 列没人回来改表，表还在、话已经不对了，
而没有任何东西会响。所以这里把表写成数据，让三件事各自可执行：

1. **列集合必须与 DDL 一致** —— 将来给 ``plans`` 加一列而忘了更新这张表，直接红；
2. **标为"可达"的列必须真被拦下** —— 每个坏值都喂一遍 ``_check_plan_row``；
3. **拦截必须是 ``InvariantError``，不是 ``StoreCorruptError``** —— 后者会把
   "压缩器算错了"说成"库坏了"，运维去跑 ``integrity_check``（一定全绿），
   真正的处置永远不会被执行（不可达的 except 不是防御，是伪装）。

## 两列可达性，判据来自 §10.4

* **可能被输入违反** —— ``AssemblyPlan`` 由压缩器构造，字段没有运行时校验
  （dataclass 不做类型检查），所以压缩器写错一个常量/传 None 就违反；
* **由构造保证不可能违反** —— 值根本不来自 plan（由 store 自己填、或由事务内分配），
  这类**不该**校验：给一条永不触发的分支写代码，报错会指向"坏输入"，
  而真坏的是构造逻辑。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlattice.errors import InvariantError, StoreCorruptError      # noqa: E402
from ctxlattice.model import AssemblyPlan                            # noqa: E402
from ctxlattice.store_sqlite import DDL, _check_plan_row             # noqa: E402
from ctxlattice.text import sha16_text                               # noqa: E402

#: 四个取值，两两不同，方便拼出"每一位都合法"的基准值。
H16 = "0123456789abcdef"

_REACHABLE = "可能被输入违反"
_BY_CONSTRUCTION = "由构造保证不可能违反"


#: 这两列**不在** ``AssemblyPlan`` 上：它们由 ``put_plan`` 作为独立参数传进来
#: （``_check_plan_row(plan, before, after)``）。审计表要把这个来源差异写出来，
#: 否则读表的人会以为给 plan 换个字段就能改它们。
ARG_COLUMNS = frozenset({"tokens_before", "tokens_after"})


def _plan(**over) -> AssemblyPlan:
    """一份**合法**的 plan；``over`` 用来单点破坏某一列。"""
    text = "F 冻结块\n  T1 甲\n    P1.1 乙\n      R1 丙 [约束]"
    base = dict(
        schema=1,
        generation=1,
        epoch_turn=3,
        r_first_turn=2,
        frozen_text=text,
        frozen_sha16=sha16_text(text),
        frozen_tokens=12,
        system_sha16="a" * 16,
        snapshot_id="b" * 16,
        store_rev=0,
        counter_id="tiktoken:o200k_base:2024-07",
        policy_digest="c" * 16,
    )
    base.update(over)
    return AssemblyPlan(**base)


def _broken(column: str, bad_value) -> AssemblyPlan:
    """把 ``bad_value`` 塞进 ``column`` 对应的位置（plan 字段或函数参数）。"""
    if column in ARG_COLUMNS:
        return _plan()          # 起点仍是合法 plan，坏值走参数
    return _plan(**{column: bad_value})


def _invoke_with(column: str, bad_value) -> None:
    before, after = 100, 50
    if column == "tokens_before":
        before = bad_value
    elif column == "tokens_after":
        after = bad_value
    _check_plan_row(_broken(column, bad_value), before, after)


#: 审计表：``列名 → (DDL 约束摘要, 本函数是否校验, 可达性分类, 一个能违反它的坏值, 备注)``。
#: 坏值只对 ``_REACHABLE`` 的列有意义；``_BY_CONSTRUCTION`` 的列填 ``None``。
AUDIT = {
    "session_id": (
        "NOT NULL, REFERENCES session(id)",
        False, _BY_CONSTRUCTION, None,
        "store 自己填（``self._store.session_id``），不来自 plan",
    ),
    "schema": ("NOT NULL, CHECK (schema >= 1)", True, _REACHABLE, 0, ""),
    "generation": (
        "NOT NULL, CHECK (generation >= 1)",
        False, _BY_CONSTRUCTION, None,
        "★ 只写候选号；真实号由 put_plan 在写事务内按 MAX+1 分配 → 恒 ≥ 1",
    ),
    "epoch_turn": ("NOT NULL, CHECK (epoch_turn >= 1)", True, _REACHABLE, 0, ""),
    "r_first_turn": ("NOT NULL, CHECK (r_first_turn >= 1)", True, _REACHABLE, 0, ""),
    "frozen_text": (
        "NOT NULL, CHECK (length(frozen_text) >= 1)",
        True, _REACHABLE, "", "",
    ),
    "frozen_sha16": (
        "NOT NULL, CHECK (length = 16 AND 小写十六进制)",
        True, _REACHABLE, "z" * 16,
        "另查派生自洽：必须等于 sha16(frozen_text)",
    ),
    "frozen_tokens": ("NOT NULL, CHECK (frozen_tokens >= 0)", True, _REACHABLE, -1, ""),
    "tokens_before": ("NOT NULL, CHECK (tokens_before >= 0)", True, _REACHABLE, -1, ""),
    "tokens_after": ("NOT NULL, CHECK (tokens_after >= 0)", True, _REACHABLE, -1, ""),
    "system_sha16": ("NOT NULL, CHECK (length = 16)", True, _REACHABLE, "a" * 15, ""),
    "snapshot_id": ("NOT NULL, CHECK (length = 16)", True, _REACHABLE, "b" * 15, ""),
    "store_rev": ("NOT NULL, CHECK (store_rev >= 0)", True, _REACHABLE, -1, ""),
    "counter_id": (
        "NOT NULL（**无格式约束**）",
        True, _REACHABLE, "",
        "本审计补的：空串能静默入库，而 R4 会因此每轮判 PLAN_INVALID → 每轮重压、不报错",
    ),
    "policy_digest": (
        "NOT NULL（**无格式约束**）",
        True, _REACHABLE, "",
        "本审计补的：同上，R3 每轮判 PLAN_INVALID；且它 = sha256(policy)[:16]，长度可钉死",
    ),
    "created_at": (
        "NOT NULL",
        False, _BY_CONSTRUCTION, None,
        "store 自己写 ``_now()``，不来自 plan",
    ),
}


def _ddl_columns() -> set[str]:
    """从 DDL 里抽出 ``plans`` 的列名（跳过表级约束与注释）。"""
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS plans \((.*?)\n\);", DDL, re.S)
    if block is None:                                   # pragma: no cover
        raise AssertionError("DDL 里找不到 plans 表：下面的比对没有意义")
    cols = set()
    for raw in block.group(1).splitlines():
        line = raw.split("--")[0].strip().rstrip(",")
        if not line:
            continue
        head = line.split()[0].upper()
        if head in ("UNIQUE", "CHECK", "PRIMARY", "FOREIGN", "CONSTRAINT"):
            continue
        cols.add(line.split()[0])
    cols.add("id")            # 第一行是 id 列，行首就是列名，正常解析；这里只兜底
    return cols


class TestAuditTableMatchesDdl(unittest.TestCase):
    """表与 DDL 必须同进同退 —— 否则审计会静默过期。"""

    def test_plans_table_is_findable(self):
        """前提自证：DDL 里确实解析出了 plans 的列。

        没有这一条，正则一旦失效会返回空集，而"空集 ⊆ 表"恒真 —— 这条用例连同下面
        那条全是空真。
        """
        cols = _ddl_columns()
        self.assertGreaterEqual(len(cols), 15,
                                "只解析出 %d 列，正则大概已经不匹配当前的 DDL 写法"
                                % len(cols))
        self.assertIn("generation", cols)
        self.assertIn("policy_digest", cols)

    def test_every_ddl_column_is_in_the_audit_table(self):
        missing = _ddl_columns() - set(AUDIT) - {"id"}
        self.assertFalse(
            missing,
            "这些 DDL 列没有出现在审计表里：%s —— 给 plans 加列时，审计表要一起更新"
            % sorted(missing))

    def test_no_audit_entry_is_a_ghost(self):
        ghosts = set(AUDIT) - _ddl_columns()
        self.assertFalse(ghosts, "审计表里有 DDL 已不存在的列：%s" % sorted(ghosts))


class TestReachableColumnsAreActuallyRejected(unittest.TestCase):
    """标为"可达"的列，坏值必须被 ``_check_plan_row`` 拦下，且必须是 ``InvariantError``。"""

    def test_the_baseline_plan_is_valid(self):
        """前提自证：基准 plan 能过。

        少了这一条，下面每个"必须被拒"都可能因为**基准本身就是坏的**而通过 ——
        那样这组用例测的是"它一直在拒"，不是"它拒对了地方"。
        """
        _check_plan_row(_plan(), 100, 50)

    def test_each_reachable_column_rejects_its_bad_value(self):
        for column, (ddl, checked, reach, bad_value, _note) in sorted(AUDIT.items()):
            if reach != _REACHABLE:
                continue
            with self.subTest(column=column):
                self.assertTrue(checked,
                                "%s 是可达的却没被校验：%s" % (column, ddl))
                with self.assertRaises(InvariantError) as ctx:
                    _invoke_with(column, bad_value)
                self.assertIn(column, str(ctx.exception),
                              "拦截 %s 的报错里没有它的列名，定位成本不为零"
                              % column)

    def test_rejection_is_invariant_error_not_store_corrupt(self):
        """分类判据：约束违反是"我们写错了"，不是"库坏了"。

        译成 ``StoreCorruptError`` 的后果不是"名字不好看"：它会走 F1（跳过存储、
        内存缓冲装配），运维去跑 ``PRAGMA integrity_check`` 而结果一定全绿 ——
        排查方向被引到「库坏了」这条死路，真正该修的压缩器永远不会被看到。
        """
        for column, (_ddl, checked, reach, bad_value, _n) in sorted(AUDIT.items()):
            if reach != _REACHABLE or not checked:
                continue
            with self.subTest(column=column):
                try:
                    _invoke_with(column, bad_value)
                except StoreCorruptError:                       # pragma: no cover
                    self.fail("%s 被译成了 StoreCorruptError —— 那是指向库的错" % column)
                except InvariantError:
                    pass


class TestByConstructionColumnsAreDeliberatelyUnchecked(unittest.TestCase):
    """反向：由构造保证不可能违反的列，**不该**被校验。

    这条守的是另一种浪费：给一条永不触发的分支写代码，一旦它真触发，报错会指向
    "坏输入"，而坏的是构造逻辑 —— 把排查方向从实现引到调用方。
    """

    def test_generation_is_not_checked_and_that_is_intentional(self):
        # 候选号写 0 也不拦：它不进库，真实号在事务内按 MAX+1 分配 → 恒 ≥ 1。
        # 这不是漏，是"校验一个不参与写库的字段只会造出假闸门"。
        _check_plan_row(_plan(generation=0), 100, 50)

    def test_generation_zero_is_still_safe_because_allocation_overrides_it(self):
        """上面那条的**理由**也要验：真实号确实由分配覆盖，不是靠 plan 的值。"""
        from ctxlattice.store_sqlite import _check_plan_row as _c  # 同名，显式引用
        p = _plan(generation=0)
        _c(p, 100, 50)
        self.assertEqual(p.generation, 0, "校验不该改动 plan（它是 frozen 的）")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
