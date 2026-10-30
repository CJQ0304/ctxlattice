# -*- coding: utf-8 -*-
"""STORE-SCHEMA.md 的可执行自检（10.0 里那些"必须挂一个可执行检查"的东西）。

    PYTHONIOENCODING=utf-8 python tools/check_doc.py

设计原则：**豁免必须被枚举出来并打印**。藏在代码里的 `if` 等于没登记过的豁免，
而没登记过的豁免连"我豁免了什么"都没人知道（顾图南：隐式豁免比显式豁免更坏）。
"""
from __future__ import annotations

import io
import os
import re
import sys

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "design", "STORE-SCHEMA.md")
lines = io.open(DOC, encoding="utf-8").read().splitlines()

# ---- 豁免登记（运行时打印，改这里必须同时改 10.0.2 的表） ----------------
EXEMPTIONS = [
    ("整节跳过 10.0（从 `### 10.0 ` 到 `### 10.1` 之前）",
     "纯规则区：本节必须能自由引用被扫字面量才讲得清，属固定污染源"),
    ("`FILE.md §` 形态的 `§` 不算裸小节号",
     "跨文档引用，本文无权校验别的小节是否存在"),
]

start = next(i for i, l in enumerate(lines) if l.startswith("### 10.0 "))
end = next(i for i, l in enumerate(lines) if l.startswith("### 10.1"))


def in_rule_zone(i: int) -> bool:
    return start <= i < end


print("=" * 74)
print("豁免登记（%d 条）" % len(EXEMPTIONS))
print("=" * 74)
for k, (what, why) in enumerate(EXEMPTIONS, 1):
    print("  %d. %s\n     └ %s" % (k, what, why))

fails = 0

# ---- 检查 1：裸小节号（未带文件名的 §） -----------------------------------
print("\n" + "=" * 74)
print("检查 1｜裸 `§N.M`（必须写成 `FILE.md §N.M`）")
print("=" * 74)
bad = []
for i, line in enumerate(lines):
    if in_rule_zone(i):
        continue
    for m in re.finditer(r"§", line):
        head = line[max(0, m.start() - 12):m.start()]
        if not re.search(r"\.md`?$", head.rstrip()):
            bad.append((i + 1, line.strip()[:90]))
for ln, text in bad:
    print("  L%-5d %s" % (ln, text))
print("  命中 %d 处 %s" % (len(bad), "[OK]" if not bad else "[!!]"))
fails += bool(bad)

# ---- 检查 2：围栏平衡 -----------------------------------------------------
print("\n" + "=" * 74)
print("检查 2｜代码围栏平衡")
print("=" * 74)
fences = sum(1 for l in lines if l.startswith("```"))
print("  ``` 计数 %d，%s" % (fences, "平衡 [OK]" if fences % 2 == 0 else "不平衡 [!!]"))
fails += fences % 2

# ---- 检查 3：同一定义只能出现一次，其余必须是指针 -------------------------
print("\n" + "=" * 74)
print("检查 3｜现行公式 `ctxsnap1` 的每一处命中都要归得了类")
print("=" * 74)
sec44 = next(i for i, l in enumerate(lines) if l.startswith("### 4.4"))
sec45 = next(i for i, l in enumerate(lines) if l.startswith("### 4.5"))
hits = [(i + 1, line) for i, line in enumerate(lines) if "ctxsnap1" in line]
n_in = 0
for ln, line in hits:
    inside = sec44 + 1 <= ln <= sec45
    points = ("4.4" in line) or ("见" in line)
    if inside:
        role = "定义区（4.4）"
        n_in += 1
        ok = True
    else:
        role = "区外引用"
        ok = points
    print("  L%-5d %-12s %s  %s" % (ln, role, "带指针" if points else "无指针",
                                    "[OK]" if ok else "[!! 无指针的第二次定义]"))
    if not ok:
        fails += 1
print("  命中 %d 处；定义区 %d 处；区外每处都带指针：%s"
      % (len(hits), n_in, "[OK]" if all(("4.4" in l or "见" in l)
                                        for ln, l in hits
                                        if not (sec44 + 1 <= ln <= sec45)) else "[!!]"))
if n_in != 1:
    print("  [!!] 定义区命中 %d 处，应恰好 1 处" % n_in)
    fails += 1

print("\n" + "=" * 74)
print("结果：%s（失败项 %d）" % ("全部通过" if not fails else "有失败", fails))
print("=" * 74)
sys.exit(1 if fails else 0)
