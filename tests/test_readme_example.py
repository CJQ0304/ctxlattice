# -*- coding: utf-8 -*-
"""README 的「快速开始」是用户第一次执行的东西 —— 它必须有一条机器守卫。

## 两层守卫：逐字执行 + 关键前提

README 那段示例现在是**整段可执行**的（provider 用本地桩 `call_provider`），
所以 `TestReadmeBlockRunsVerbatim` 直接把代码块从 README 里抽出来 `exec` ——
不是"照它另写一份"。这样"改 README 而没跑通"会立刻红。

（原先那段里有 `client.chat(...)`，整段没法执行，只能钉"最易烂的那一半"。
把桩写进 README 是更硬的修法：**一条"跑不了"的示例，无论钉多少条断言，
都还是没人真正跑过它**。）

下面这组断言守着两个看起来"理所当然"的名字：

| 处 | 照抄写错 | 后果 |
|---|---|---|
| 类名 | `SQLiteStore` | `ImportError`，第一步就断 |
| 归因行 | `"  P1.1 事件节点"` | `AttributionError`，用户以为是 bug |

这两处的共同点是：**它们错了不会报"文档旧了"，只会报"这库坏了"**。而源码树里
没有任何一条用例会因为它们错而红 —— 这正是它们能烂掉的原因。

## 第二条比第一条重要：拒绝的**理由**

`P1.1` 被拒不是"格式洁癖"，是 P3：编号由 SDK 分配，**不复用、不重排**。
调用方一旦能自己指定编号，两次 `attribute()` 就可能在同一棵树上产生两个 `P1`，
或者让已有的 `P1` 因为新插入的 `P0` 而改名 —— 那时 `plans` 里冻结的 F 与当前键树
对不上，**没有任何断言会响**。

所以下面除了断言"拒绝"，还断言**消息里带着这条理由**。理由比校验本身重要：
下一个人看到"用户写了 `P1.1` 就被拒"，最自然的反应是放宽实现让它通过，
而放宽等于把 P3 静默扔掉。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctxlattice                                                     # noqa: E402
from ctxlattice import Policy, Session, SqliteStore                   # noqa: E402
from ctxlattice.errors import AttributionError                        # noqa: E402


class _TempDb(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "session.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestReadmeShapes(_TempDb):
    def test_style_number_is_rejected(self):
        """`P1.1` 必须被拒，且**消息里要带上理由**（编号由 SDK 分配）。"""
        store = SqliteStore(self.path, store_id="demo", session_id="s1")
        session = Session.open(store=store, policy=Policy(budget_tokens=8000))
        try:
            with self.assertRaises(AttributionError) as cm:
                session.attribute(["T 甲", "  P1.1 乙"])
        finally:
            session.close()

        message = str(cm.exception)
        # 存在性自证：先确认被拒的是**这一行**，否则下面两条可能是别的行给的提示。
        self.assertIn("P1.1", message, "报错里没有回显被拒的那一行：%r" % message)
        self.assertIn("标记后必须紧跟一个空格", message,
                      "报错没告诉用户**怎么改**：%r —— 只说「无法识别」等于让用户去读源码"
                      % message)
        self.assertIn("编号由 SDK 分配", message,
                      "报错没给**理由**：%r。下一个人会以为这只是格式洁癖，"
                      "于是放宽实现让 `P1.1` 通过 —— 那等于放弃 P3"
                      "（编号不复用、不重排）" % message)

    def test_the_readme_example_shape_actually_works(self):
        """照 README 的形状跑一遍：类名、构造参数、归因行都必须**逐字可用**。"""
        # 前提自证：这两条名字就是"照抄会错"的那两个。先钉住正确名字存在、
        # 错误名字不存在 —— 后半句是给"哪天有人加了个兼容别名"留的提醒位：
        # 加了别名，README 的错误写法就又能跑，而这条用例会先响。
        self.assertTrue(hasattr(ctxlattice, "SqliteStore"), "README 用的类名不存在了")
        self.assertFalse(hasattr(ctxlattice, "SQLiteStore"),
                         "多了一个 `SQLiteStore` 别名：README 里那个错误写法从此又能跑，"
                         "而「照抄就是错的」这件事再也不会被发现")

        store = SqliteStore(self.path, store_id="demo", session_id="s1")
        session = Session.open(store=store, policy=Policy(budget_tokens=8000))
        try:
            payload = session.send("把 E 行的摘要规则再讲一遍")
            self.assertTrue(payload.to_bytes())
            self.assertFalse(payload.degraded, "README 的示例不该一上手就降级")
            session.observe("摘要禁半角竖线。")

            stat = session.attribute(
                ["T 存储格式", "  P 事件节点", "    R 摘要禁半角竖线 [约束]"])
        finally:
            session.close()

        self.assertIsNotNone(stat, "attribute() 返回 None：存储写失败（README 示例跑不通）")
        self.assertEqual((stat.t_new, stat.p_new, stat.r_new), (1, 1, 1),
                         "README 那三行增量应当建出 1 个 T / 1 个 P / 1 条 R，实测 %r"
                         % (stat,))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# 逐字执行：README 里那段代码块，原样抽出来跑
# ---------------------------------------------------------------------------
README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "README.md")


def _readme_python_block() -> str:
    """抽出「快速开始」下面那个 ```python 代码块。

    按小节定位而不是"第一个 python 块"：README 里还有别的代码块（bash / 表格），
    用"第一个"会在别人往前面插一段示例时静默换掉被测对象。
    """
    text = open(README, encoding="utf-8").read()
    start = text.index("## 快速开始")
    fence = text.index("```python", start)
    body = fence + len("```python")
    end = text.index("```", body)
    return text[body:end]


class TestReadmeBlockRunsVerbatim(_TempDb):
    """★ 把 README 的代码块原样 `exec`；不是抄一遍。"""

    def test_the_block_is_extracted_from_the_right_section(self):
        """前提自证：抽出来的确实是那段示例，不是别的东西。

        没有这一条，「抽到空串 + exec 空串成功」也能绿 —— 空真。
        """
        code = _readme_python_block()
        self.assertIn("SqliteStore(", code)
        self.assertIn("session.attribute(", code)
        self.assertIn("session.close()", code)
        self.assertEqual(code.count("```"), 0, "代码块没截干净，把围栏也吃进去了")

    def test_the_block_compiles(self):
        compile(_readme_python_block(), "<README 快速开始>", "exec")

    def test_the_block_actually_runs(self):
        """真跑一遍。目录切到临时目录，示例里写的 `session.db` 就落在那里。"""
        code = compile(_readme_python_block(), "<README 快速开始>", "exec")
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            namespace: dict = {"__name__": "__readme_example__"}
            exec(code, namespace)                       # noqa: S102 - 被测对象就是它
        finally:
            os.chdir(cwd)

        # 跑完必须留下痕迹：否则"什么都没做也没报错"同样能绿。
        self.assertTrue(os.path.exists(self.path),
                        "示例跑完没有生成 session.db：它大概没真的走存储路径")

        # 再独立打开一次，确认库里确实有一轮对话 + 一条归因（而不是只建了个空库）。
        # ⚠️ `store_id` 必须与示例里一致（"demo"）：`store` 表按 `store_id` 建行，
        # 换一个 id 会读到一个**新建的空 store**，于是 rev=0 被误读成"归因没落库"。
        # 这不是库的行为奇怪，是"同一个库文件里可以有多个 store"——
        # 用它当证据时，坐标必须写全。
        store = SqliteStore(self.path, store_id="demo", session_id="s1")
        try:
            snap = store.snapshot()
            self.assertEqual(snap.turn_hi, 1, "示例应该只写 1 轮")
            # 归因必须真的落库。`rev` 只在内容指纹真变时 +1（P4），
            # 所以它是"那条 R 确实进去了"的可观测证据 —— 而"库文件存在"不是：
            # 建个空库也能让上一句 `exists` 通过。
            self.assertGreater(snap.rev, 0,
                               "库里 rev = 0：文件建出来了，但那条归因没落库")
        finally:
            store.close()
