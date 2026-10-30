# -*- coding: utf-8 -*-
"""交付件的形状：`packages` 必须覆盖**每一个**子包。

## 这条用例守的是一个"在源码树里永远绿"的缺陷

`pyproject.toml` 原来写 `packages = ["ctxlattice"]`，而 `ctxlattice/bench/` 是子包。
于是：

* 源码树里 `python -m ctxlattice.bench` **照跑** —— 因为 cwd 在 `sys.path` 上；
* `pip install .` 之后 `python -m ctxlattice.bench` → `No module named ctxlattice.bench`。

**没有任何一条用例会因为"装出来没有 bench"而红**，因为所有用例都跑在源码树里。
而这个包的全部卖点是「能真实测出省多少 token」—— bench 不在安装件里，
"可测量"就只剩源码树里成立。判据：**凡是"在源码树里能跑"的东西，都要在 wheel 里再验一遍。**

## 这条用例的局限（写在明处，免得被当成"装出来一定对"）

它**只读配置，不 build wheel**：不用网、几十毫秒。所以它证明的是
「子包都被显式登记了」，不是「wheel 里真的有那些文件」。后者需要真装一遍
（`pip install . --no-deps --target <dir>`），代价是要几十秒。
这里选择便宜的判据，理由是**这个缺陷的具体形状就是"忘了登记子包"** ——
登记了却装不进去，是 setuptools 自己的问题，不在本仓库的控制范围内。
读的人应当知道这条分界在哪里。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(ROOT, "ctxlattice")


def _declared_packages() -> list[str] | None:
    """读 `[tool.setuptools] packages`。返回 ``None`` 表示**没有显式声明**。

    优先用 ``tomllib``（3.11+）；3.10 没有它，退回一个只认这一行的正则 ——
    够用，且不会因为 "某个别的表里也有 packages" 而误判（锚在行首）。
    """
    path = os.path.join(ROOT, "pyproject.toml")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        import tomllib
    except ModuleNotFoundError:                                   # pragma: no cover - 3.10
        text = raw.decode("utf-8")
        m = re.search(r"^packages\s*=\s*\[(.*?)\]", text, re.S | re.M)
        if m is None:
            return None
        return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    data = tomllib.loads(raw.decode("utf-8"))
    packages = (data.get("tool", {}).get("setuptools", {}) or {}).get("packages")
    return list(packages) if packages is not None else None


def _subpackages() -> list[str]:
    """扫出**源码树里真实存在**的子包（带 ``__init__.py`` 的目录），不含根包。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if dirpath == PACKAGE_DIR:
            continue
        if "__init__.py" in filenames:
            rel = os.path.relpath(dirpath, os.path.dirname(PACKAGE_DIR))
            found.append(rel.replace(os.sep, "."))
    return sorted(found)


class TestPackagesCoverEverySubpackage(unittest.TestCase):
    def test_the_premise_holds(self):
        """自证前提：**先证明真的有子包可查**。

        没有子包时，"每个子包都登记了"是空真 —— 而这条用例的全部价值
        就建立在那唯一的子包（``ctxlattice.bench``）上。它哪天被删掉或者改名，
        这里要立刻响，而不是继续绿。
        """
        self.assertTrue(os.path.isdir(PACKAGE_DIR), "包目录不存在：%s" % PACKAGE_DIR)
        self.assertIn("ctxlattice.bench", _subpackages(),
                      "扫不到 `ctxlattice.bench` —— 要么它没了，要么扫的方式坏了")
        self.assertTrue(
            os.path.isfile(os.path.join(PACKAGE_DIR, "bench", "__init__.py")),
            "`ctxlattice/bench/__init__.py` 不存在：`python -m ctxlattice.bench` "
            "要它 + `__main__.py` 才成立")
        self.assertTrue(
            os.path.isfile(os.path.join(PACKAGE_DIR, "bench", "__main__.py")),
            "`ctxlattice/bench/__main__.py` 不存在：`-m` 入口就在这里")

    def test_every_subpackage_is_declared(self):
        declared = _declared_packages()
        self.assertIsNotNone(
            declared,
            "pyproject.toml 没有显式声明 `[tool.setuptools] packages`。"
            "本用例的判据只对显式声明成立 —— 请显式列出根包与每个子包"
            "（理由见 pyproject.toml 里那段注释：源码树能跑 ≠ wheel 里有）")

        self.assertIn("ctxlattice", declared,
                      "根包都没登记：%r" % (declared,))

        missing = sorted(set(_subpackages()) - set(declared))
        self.assertEqual(
            missing, [],
            "这些子包**没有**登记进 `packages`：%r（当前 %r）。"
            "后果不是 ImportError 而是「装到别处就没有」：源码树里照跑，"
            "因为 cwd 在 sys.path 上 —— 所以没有任何用例会红，直到有人装它"
            % (missing, declared))


if __name__ == "__main__":
    unittest.main(verbosity=2)
