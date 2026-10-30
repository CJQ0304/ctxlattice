"""A7 的静态闸门：审计字段不得参与控制流。

这条纪律**靠人 review 不可能守住**，因为违规写法的共同特征是「看起来很自然」：

    if plan.store_rev != snapshot.rev:      # 「库变了，plan 过期了」——非常合理，非常错
    if store.updated_at < plan.epoch_turn:  # 「上次更新早于边界」——同上
    if plan.snapshot_id != snapshot.snapshot_id:  # 绕开 system_sha16 的重复表达

后果统一是：**每一轮都判 plan 失效 → 每轮重压 → 每轮全价轮**，
而表面上一切正常（payload 照常产出，不报错不崩溃），只是成本静默翻几倍。

所以这里用**静态扫描**把它挡在 CI 里。局限也要说清：
* 只认文本模式，认不出经过变量中转的写法（`x = p.store_rev; if x != y`）；
* 因此它是**下限**，不是证明。真正的保证是设计上把这些字段排除在判据之外。

    python -m unittest tests.test_audit_fields -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "ctxlattice"

#: 三个审计字段。第三个（store.updated_at）现在没人拿它判定，但
#: 「上次更新早于边界，所以 plan 过期」是个非常自然、非常错的推断，早晚有人写。
AUDIT_FIELDS = ("snapshot_id", "store_rev", "updated_at")

#: 允许出现的文件：**豁免必须显式登记并附理由**（宋砚秋的验收口径——
#: 豁免一旦不需要成本就等于取消检查，因为加一行注释比改代码便宜太多）。
#: 想新增一条豁免，必须在 reason 里写清「它为什么不是判定」。
ALLOWED_FILES = {
    "store_sqlite.py": "落库与读出（写路径），不在此做有效性判断",
    "store.py": "StorePort 协议字段声明，无逻辑",
    "model.py": "字段定义处",
    "compaction.py": "构造 plan 时**写入**，不许从 plan 读出来判",
    "session.py": "把 plan 交给 StoreTxn 落盘，只传不判",
}

#: **判断节点**：只有这几类 AST 节点在"做决定"。出现在它们里面的审计字段就是违规。
#: 为什么按节点类型而不是按行扫 —— 见 ``_violations_in_source`` 的说明。
_JUDGEMENT_NODES = (
    ast.Compare, ast.If, ast.While, ast.Assert, ast.IfExp, ast.BoolOp,
)


def _violations_in_source(src: str, filename: str = "<src>") -> list[str]:
    """在**可执行代码**里找审计字段，不扫注释与字符串。

    这里从"按行正则"改成了"按 AST 节点"，原因是原来那版有一个**隐式豁免**：
    它用一个 `startswith` 判断跳过注释行与 docstring 的**首行**，正文照扫。

    两头都不对：

    * 松的那头 —— 违规写进注释就不会被报。注释不执行，松得有道理，但**没人知道
      它被豁免了**，因为它长得像一句合理的格式判断。隐式豁免比显式豁免更坏：
      显式豁免至少登记过，隐式豁免连"我豁免了什么"都没人知道；
    * 紧的那头 —— 注释里**描述**这个反模式的代码会被当真违规报出来。这正是
      "检查项的自述污染检查"：规则正文天生要提到被扫目标。

    两个方向混在一起时，处置必然是"改措辞让它别命中"，而改措辞治不了"必须提到"。
    唯一能终止这件事的做法是**换判据**：不看字符串，看语法树 —— docstring 是
    常量表达式，永远不会出现在判断节点里，天然不命中，**无需任何豁免**。

    （本段刻意不复述那个 `startswith` 的字面量。第一版复述了，于是三个引号把
    本 docstring 自己截断、文件语法错误 —— 同一个坑，隔一段又踩了一次。）

    判据仍然是"出现在判断里"，不是"出现了"。``model.py`` 里写字段定义、
    ``compaction.py`` 里写入、``store_sqlite.py`` 里落库，都不算违规。
    """
    tree = ast.parse(src, filename=filename)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _JUDGEMENT_NODES):
            continue
        # 只看这一棵判断子树里的名字，不再往外扩
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                name = sub.id
            elif isinstance(sub, ast.Attribute):
                name = sub.attr
            else:
                continue
            if name in AUDIT_FIELDS:
                found.add((getattr(sub, "lineno", 0), name))
    return ["%s:%d  %s 出现在判断表达式里" % (filename, lineno, name)
            for lineno, name in sorted(found)]


def _violations() -> list[str]:
    out: list[str] = []
    for py in sorted(PKG.glob("*.py")):
        if py.name in ALLOWED_FILES:
            continue
        out.extend(_violations_in_source(py.read_text(encoding="utf-8"), py.name))
    return out


class TestAuditFieldsNotControlFlow(unittest.TestCase):
    def test_no_audit_field_in_conditionals(self):
        bad = _violations()
        self.assertEqual(
            bad, [],
            "审计字段出现在判定里（A7）。这些字段在正常运行时**会合法地变化**，"
            "拿它做闸门会让每轮都变成边界轮，且不报错：\n  " + "\n  ".join(bad))

    def test_scanner_has_teeth(self):
        """负向注入：确认扫描器真的会红。

        没有正控的检查器永远返回通过，你无法区分「真没问题」与「根本没在跑」。
        """
        sample = "def f(plan, snap):\n    if plan.store_rev != snap.rev:\n        return 1\n"
        self.assertTrue(_violations_in_source(sample),
                        "扫描器对这个明显违规样本一声不响 —— 它没在工作")

    def test_scanner_is_not_fooled_by_prose(self):
        """★ 反向正控：**描述**这个反模式的注释与 docstring 不算违规。

        这一条与上一条同等重要，而且更少见人写。原实现用一个 `startswith`
        判断跳过注释行与 docstring 首行、正文照扫，于是"别这样写：`if plan.store_rev
        != ...`"这种**说明**会被自己判成违规；接着的处置必然是改措辞，而改措辞
        治不了"规则正文必须提到被扫目标"。

        AST 判据下这类文本是常量表达式，天然不在判断节点里。

        （本段同样刻意不复述那个字面量 —— 见 `_violations_in_source` 的说明。）
        """
        prose = (
            'def f(plan):\n'
            '    """别这样写：\n'
            '        if plan.store_rev != snapshot.rev:   # 这会让每轮都变全价轮\n'
            '    """\n'
            '    # if plan.store_rev != snapshot.rev:  注释里的反例同样不该报\n'
            '    return plan.topology\n'
        )
        self.assertEqual(_violations_in_source(prose), [],
                         "注释/docstring 里的示例被当成了违规 —— 检查器会开始污染自己")

    def test_scanner_sees_multiline_and_truthiness(self):
        """正则版漏掉的两种形态，AST 版必须看得见。"""
        cases = {
            "跨行比较": (
                "def f(p, s):\n"
                "    if (\n"
                "        p.store_rev\n"
                "        != s.rev\n"
                "    ):\n"
                "        return 1\n"),
            "truthiness": "def f(p):\n    if p.updated_at:\n        return 1\n",
            "三元表达式": "def f(p):\n    return 1 if p.store_rev else 2\n",
            "assert": "def f(p):\n    assert p.snapshot_id\n",
        }
        for label, src in cases.items():
            self.assertTrue(_violations_in_source(src), "漏判：%s" % label)


class TestAuditListIsNotAVibe(unittest.TestCase):
    """哪些字段算「审计字段」，本身也要有判据，不能凭名字。

    判据只有一条（A7）：**这个字段在正常运行中会不会合法地变化？**

    * 会变 → 审计字段，只许写入与输出；
    * 不会变 → 可以参与控制流。

    一个字段最容易混进来的方式是**名字像审计字段**，而不是它真的只写不判。
    """

    def test_content_hash_is_not_an_audit_field(self):
        """`turn.content_hash` **不是**审计字段 —— 它是 P9 检出②的探针。

        名字像"只记录不判断"，但按 A7 判据它不是：已提交轮次的内容**不该**
        合法变化，所以"我发的是 A、库里存的是 B"是一个有效的判据。

        为什么单独立一条：F16 的检出②（两个驱动错开了 `generation`，唯一键冲突
        永不触发，只能靠"这一轮内容对不上"发现）**完全依赖它参与比较**。
        若有人按名字把它塞进 `AUDIT_FIELDS`，P9 的静默失效路径就被封死了 ——
        而且封得很自然，因为"内容哈希当然不该拿来判断"听起来是常识。
        """
        self.assertNotIn(
            "content_hash", AUDIT_FIELDS,
            "content_hash 被登记成了审计字段 —— 那会让 F16 的检出②无法实现。"
            "它不满足 A7 判据（已提交轮次的内容不该合法变化）。")

    def test_audit_list_is_explicit(self):
        """审计清单必须短且可枚举：长到读不完就等于没定义。"""
        self.assertLessEqual(len(AUDIT_FIELDS), 5,
                             "审计字段超过 5 个，先问每个是不是真的'会合法变化'")
        for f in AUDIT_FIELDS:
            self.assertTrue(f and f.isidentifier(), "字段名不合法：%r" % (f,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
