# -*- coding: utf-8 -*-
"""依赖纪律的**可执行**版本（ARCHITECTURE §3.2 第 2–4 条）。

这些规则不能靠 review 守住，因为它们全都是"加一行 import"级别的改动：

* ``assembler`` 一旦 import ``store``，F 的字节就会在边界之间随归因写入变化，
  ``cached_i`` 归零，**且没有任何报错**。所以它连 ``os`` / ``time`` / ``random``
  都不许碰 —— 那三样是"顺手拿个时间戳/临时文件名"的入口；
* 只有 ``store_sqlite`` 能 import ``sqlite3``：换存储实现时，"哪里依赖 SQLite"
  必须是一个可以 grep 出答案的问题；
* 只有 ``tokens`` 能 import ``tiktoken``，且在 ``try/except ImportError`` 之后：
  tiktoken 是可选依赖，缺了要能降级到 estimate 而不是 import 崩。

用 AST 而不是正则：正则会被注释和字符串里的单词骗到。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "ctxlattice")


def modules_from_source(source: str, filename: str = "<string>", *,
                        runtime_only: bool = True) -> set[str]:
    """从源码文本收集 import 的顶层模块名。

    与 ``modules_imported`` 分开，是为了**检测器本身能被测**：正控要喂一段
    合成的源码，而不是往仓库里写文件。
    """
    tree = ast.parse(source, filename=filename)

    type_checking_lines: set[int] = set()
    if runtime_only:
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                    and node.test.id == "TYPE_CHECKING":
                for child in node.body:
                    for item in ast.walk(child):
                        type_checking_lines.add(getattr(item, "lineno", -1))

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.lineno in type_checking_lines:
                continue
            if node.level:                      # 相对导入：记成 ".name"
                names.add("." + (node.module or ""))
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def modules_imported(path: str, *, runtime_only: bool = True) -> set[str]:
    """收集某个文件 import 的顶层模块名。

    ``runtime_only=True`` 时跳过 ``if TYPE_CHECKING:`` 块：那里只是给类型标注用的，
    运行期不产生任何依赖（``assembler`` 用 ``FrozenBlock`` 做注解是合法的，
    它并没有 import freeze 的运行时）。
    """
    with open(path, "r", encoding="utf-8") as handle:
        return modules_from_source(handle.read(), filename=path,
                                   runtime_only=runtime_only)


def package_files(*, include_bench: bool = False) -> list[str]:
    """包内 ``.py`` 文件的绝对路径。

    ``include_bench=True`` 时把 ``ctxlattice/bench/`` 子包一并纳入。
    默认不含，是因为有一条用例问的恰恰是「**包内有没有模块 import bench**」,
    把 bench 自己算进来那句话就没意义了。

    但这个开关的存在本身就是一条缺陷的证据：``os.listdir(PKG)`` 只看**顶层**,
    「只有 ``store_sqlite`` 能 import ``sqlite3``」这条规则长期只覆盖了包的一部分,
    而**扫描器扫 0 个文件时同样返回空 offenders、同样绿**。子包里的代码是
    真实会被 import 的代码，纪律必须跟着走到那里。
    """
    files = sorted(
        os.path.join(PKG, name)
        for name in os.listdir(PKG)
        if name.endswith(".py")
    )
    if include_bench:
        bench_dir = os.path.join(PKG, "bench")
        if os.path.isdir(bench_dir):
            files.extend(sorted(
                os.path.join(bench_dir, name)
                for name in os.listdir(bench_dir)
                if name.endswith(".py")
            ))
    return files


def import_closure(module: str, *, runtime_only: bool = True) -> set[str]:
    """``module``（包内相对名，如 ``"assembler"``）的**传递** import 闭包。

    为什么要算闭包而不是只看直接 import：删掉一行直接 import、改成经中间模块
    间接依赖，直接扫描会变绿而依赖关系一点没变。L2 之间的那条禁令尤其如此 ——
    ``assembler`` 不 import ``freeze`` 但 import 了一个 import ``freeze`` 的模块，
    效果完全一样，而 A3 从第二层就破了（``freeze`` 会把 ``relevance`` 与检索
    入口一起带进装配侧）。

    返回集合里的相对模块保留前导 ``.``（与 ``modules_imported`` 一致）。
    """
    seen: set[str] = set()
    queue: list[str] = [module]
    while queue:
        current = queue.pop()
        path = os.path.join(PKG, current + ".py")
        if not os.path.isfile(path):
            continue
        for name in modules_imported(path, runtime_only=runtime_only):
            if name in seen:
                continue
            seen.add(name)
            if name.startswith("."):
                queue.append(name[1:])
    return seen


class TestDependencyDiscipline(unittest.TestCase):
    def test_only_store_sqlite_imports_sqlite3(self):
        offenders = [os.path.basename(p) for p in package_files(include_bench=True)
                     if "sqlite3" in modules_imported(p)]
        self.assertEqual(offenders, ["store_sqlite.py"],
                         "只有 store_sqlite.py 可以 import sqlite3：%s" % offenders)

    def test_only_tokens_imports_tiktoken(self):
        offenders = [os.path.basename(p) for p in package_files(include_bench=True)
                     if "tiktoken" in modules_imported(p)]
        self.assertEqual(offenders, ["tokens.py"],
                         "只有 tokens.py 可以 import tiktoken：%s" % offenders)

    def test_deep_scan_actually_reaches_the_bench_subpackage(self):
        """正控：``include_bench=True`` 必须**真的**多扫到文件。

        这条不是形式主义。``os.listdir(PKG)`` 只列包顶层的 ``.py``，
        ``ctxlattice/bench/`` 整个子包从来没进过扫描范围 —— 也就是说
        「只有 store_sqlite 能 import sqlite3」这条规则实际只覆盖了包的一部分，
        而且**漏了看不出来**：扫描器扫 0 个文件时返回空 offenders，照样是绿的。
        所以"干净"这个结论必须配一条"扫描面确实覆盖到了"的证明。
        """
        shallow = {os.path.basename(p) for p in package_files()}
        deep = {os.path.basename(p) for p in package_files(include_bench=True)}
        added = deep - shallow
        self.assertTrue(added, "include_bench=True 没有多扫到任何文件")
        self.assertIn("harness.py", added,
                      "deep 扫描应当包含 ctxlattice/bench/harness.py：%s" % sorted(added))

    def test_detector_catches_a_planted_sqlite3_import(self):
        """正控：检测器本身有效（否则上面那个空 offenders 不代表"干净"）。

        与上一条互补：上一条证明**扫描面**够，这一条证明**判据**够。
        两条合起来才等于"bench 里塞一个 ``import sqlite3`` 会被抓住"。
        """
        planted = modules_from_source(
            "import json\n"
            "import sqlite3\n"
            "from ..store import MemoryStore\n",
            filename="<planted>")
        self.assertIn("sqlite3", planted)
        self.assertIn(".store", planted)

    def test_tiktoken_is_guarded_by_try_except_import_error(self):
        with open(os.path.join(PKG, "tokens.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for child in ast.walk(node):
                    if (isinstance(child, ast.Import)
                            and any(a.name.split(".")[0] == "tiktoken" for a in child.names)):
                        guarded = any(
                            isinstance(handler.type, ast.Name)
                            and handler.type.id == "ImportError"
                            for handler in node.handlers if handler.type is not None)
        self.assertTrue(guarded,
                        "tiktoken 必须包在 try/except ImportError 里：它是可选依赖，"
                        "缺了要降级到 estimate，而不是 import 就崩")

    def test_assembler_is_pure(self):
        """``assembler`` 不得 import 存储 / 时钟 / os / time / random。"""
        # ``.freeze`` 必须显式列进来：它和 assembler **同在 L2**，所以
        # "L(n) 不得 import L(n+1)" 那条层级规则抓不到同级依赖 —— 这就是它
        # 当初漏网的原因。凡是"会让 F 变成现场可算"的模块，一律按名字禁。
        forbidden = ("sqlite3", "os", "time", "random", "subprocess", "datetime",
                     ".store", ".store_sqlite", ".clock", ".session", ".freeze",
                     ".compaction", ".relevance", ".degrade", ".events", ".arms")
        found = modules_imported(os.path.join(PKG, "assembler.py"))
        violations = sorted(name for name in found
                            if name in forbidden or any(name.startswith(f) for f in (".store",)))
        self.assertEqual(violations, [],
                         "assembler 必须只接受已经取好的数据（A3）：%s" % violations)

    def test_assembler_transitive_closure_is_clean(self):
        """★★ 传递闭包版：``assembler`` 间接依赖也不许碰到存储 / 冻结构建。

        直接 import 干净≠依赖干净：``assembler → x → freeze`` 与
        ``assembler → freeze`` 在 A3 上是同一件事（``freeze`` 会把 ``relevance``
        和检索入口一起拖进来，装配侧于是有了"现场算 F"的能力）。
        这条用例就是防止"删掉直接 import、绕道间接依赖"这种修法蒙混过关。
        """
        closure = import_closure("assembler")
        banned = (".freeze", ".store", ".store_sqlite", ".clock", ".session",
                  ".compaction", ".relevance", ".degrade", ".events", ".arms",
                  "sqlite3", "os", "time", "random", "subprocess", "datetime")
        hit = sorted(name for name in closure
                     if name in banned or any(name.startswith(b) for b in (".store",)))
        self.assertEqual(hit, [],
                         "assembler 的**传递** import 闭包里出现了 %s："
                         "装配侧不得有任何取数或现场构建 F 的路径" % hit)

    def test_render_and_assembler_are_the_only_byte_producers(self):
        """生成 payload 字节的只能有 render / assembler（其余模块只产出结构化片段）。"""
        found = modules_imported(os.path.join(PKG, "assembler.py"))
        self.assertIn(".render", found, "assembler 必须通过 render 取片段，不要自己拼")
        self.assertNotIn(".freeze", found,
                          "assembler 不得 import freeze：F 的字节来自 plan，不是现场构建")

    def test_assembler_has_zero_reference_to_freeze(self):
        """连 ``if TYPE_CHECKING:`` 里的 ``from .freeze import ...`` 也不许有。

        比上一条更严，而且是有理由的严：类型标注是"最容易被顺手扩成运行时调用"
        的地方 —— 今天只是 ``FrozenBlock`` 做注解，明天有人想算 ``TIER_ORDER``
        就会把那一行挪出 ``if TYPE_CHECKING``，动静小到没有 diff 感。
        所以 assembler 需要的不是"少依赖一点"，而是**零引用**：它只要 F 的正文，
        就在自己文件里声明 ``FrozenText`` 协议（结构化子类型，``FrozenBlock`` 天然满足）。
        "看不见构建函数"比"保证不调用"更难被破坏。
        """
        raw = modules_imported(os.path.join(PKG, "assembler.py"), runtime_only=False)
        self.assertNotIn(".freeze", raw,
                         "assembler 对 freeze 必须零引用（含类型标注）：F 的字节只来自 plan")

    def test_no_layer_imports_bench(self):
        offenders = [os.path.basename(p) for p in package_files()
                     if any(name.startswith(".bench") or name == "bench"
                            for name in modules_imported(p))]
        self.assertEqual(offenders, [], "包内任何模块都不得 import bench：%s" % offenders)


if __name__ == "__main__":
    unittest.main(verbosity=2)
