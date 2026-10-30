"""增量校验与幂等归并（``ctx.py`` ``apply_delta`` 的行为契约，一字不改）。

本模块**只校验 + 归并，不生成内容**（A1：智能在提示里，代码只记账）。
它拿到的每一行都是调用方（模型）已经判定好的 T/P/R/E，SDK 不猜、不补、不合并语义。

行为契约（逐条对应 ``ctx.py``，任何一条改了都会让「记账」变成「理解」）：

* **同文本 + 新标记 = 改标记**，不是重复。否则 ``[已废]`` 这类审计标记在首次写入
  之后将永远改不了，文档承诺的留痕机制就落不了地。
* 纯重复 → ``r_dup`` / ``e_dup``，``changed=False``，**不写盘、不 +rev**（P4）。
* ``E`` 行按 ``(归一化摘要, src)`` 定位；``sha`` / ``bytes`` 变化 = ``e_upd``。
* 无标记（``tag is None``）意味着「不动标记」，**不是**「把标记清空」。
  这条与 ``ctx.py`` 一致，也是 SCHEMA R2 的坑：写成 ``SET tag = excluded.tag``
  会把已有的 ``[钉住]`` 清成 NULL，后果是用户明确钉住的内容被静默折叠掉。

依赖：``errors`` / ``model`` / ``text``。**不 import 存储 / 时钟 / 渲染。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Sequence

from .errors import AttributionError
from .model import DeltaKind, DeltaOp, Leaf, MergeStat, Purpose, Topic
from .text import (
    EVENT_SHA_LEN,
    body_key as _content_fingerprint,
    check_sha16,
    check_src,
    check_summary,
    check_tag,
    check_text,
    normalize,
    sanitize,
    split_tag,
)

if TYPE_CHECKING:
    from .model import StoreSnapshot

#: E 行四段：摘要（非贪婪）→ src（贪婪，允许空格）→ bytes（锚定）→ sha256（锚定）。
#: 摘要非贪婪所以写入侧禁半角竖线；src 贪婪所以 Windows 路径里的空格不会切断。
RE_EVENT_META = re.compile(
    r"^E (.+?) \| src:(.+) \| bytes:(\d+) \| sha256:([0-9a-f]{%d})$" % EVENT_SHA_LEN
)

RE_SIGIL = re.compile(r"^(T|P|R|E)(?:[ \t]+(.*))?$")
RE_LOOKS_LIKE_SIGIL = re.compile(r"^[TPRE][0-9]")


# ---------------------------------------------------------------------------
# 编号分配
# ---------------------------------------------------------------------------
class NumberAllocator:
    """只增不减的编号分配器（SCHEMA §3 ``num_alloc`` 的内存版）。

    为什么不能靠 ``MAX(num)+1``：``MAX`` 的语义是「现存行里最大的号」，它依赖行的
    **存活**。一旦某行被物理删除，``MAX`` 就会给出一个**曾经用过**的号，而复用的
    后果是外部引用（索引、导出 JSON、人的笔记里的 ``T2``）静默指向另一个事物，
    **没有任何报错**（SCHEMA R3）。

    不传分配器时走 ``ctx.py`` 的「最小未用正整数」——两者在「键层行永不物理删除」
    的前提下等价，而后者是原格式的既定行为，必须保留以复现旧库。
    """

    def __init__(self, topic_last: int = 0, purpose_last: dict[int, int] | None = None) -> None:
        self._topic_last = max(0, topic_last)
        self._purpose_last = {int(k): int(v) for k, v in (purpose_last or {}).items()}

    def topic(self) -> int:
        self._topic_last += 1
        return self._topic_last

    def purpose(self, topic_num: int) -> int:
        nxt = self._purpose_last.get(topic_num, 0) + 1
        self._purpose_last[topic_num] = nxt
        return nxt

    def snapshot_topic_last(self) -> int:
        return self._topic_last

    def snapshot_purpose_last(self) -> dict[int, int]:
        return dict(self._purpose_last)


def _smallest_unused_topic(topics: Sequence["_MTopic"]) -> int:
    used = {t.num for t in topics}
    n = 1
    while n in used:
        n += 1
    return n


def _smallest_unused_purpose(topic: "_MTopic") -> int:
    used = set()
    for p in topic.purposes:
        try:
            used.add(int(p.num.split(".")[1]))
        except (ValueError, IndexError):
            continue
    n = 1
    while n in used:
        n += 1
    return n


# ---------------------------------------------------------------------------
# 可变中间结构（Topic 是 frozen，归并需要临时可变树）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _MLeaf:
    kind: str
    text: str
    tag: str | None
    src: str | None = None
    nbytes: int | None = None
    sha16: str | None = None


@dataclass(slots=True)
class _MPurpose:
    num: str
    name: str
    tag: str | None
    leaves: list[_MLeaf] = field(default_factory=list)


@dataclass(slots=True)
class _MTopic:
    num: int
    name: str
    tag: str | None
    purposes: list[_MPurpose] = field(default_factory=list)


def to_mutable(topics: Iterable[Topic]) -> list[_MTopic]:
    out: list[_MTopic] = []
    for t in topics:
        mt = _MTopic(num=t.num, name=t.name, tag=t.tag)
        for p in t.purposes:
            mp = _MPurpose(num=p.num, name=p.name, tag=p.tag)
            for leaf in p.leaves:
                mp.leaves.append(_MLeaf(
                    kind=leaf.kind, text=leaf.text, tag=leaf.tag,
                    src=leaf.src, nbytes=leaf.nbytes, sha16=leaf.sha16,
                ))
            mt.purposes.append(mp)
        out.append(mt)
    return out


def to_frozen(topics: Iterable["_MTopic"]) -> list[Topic]:
    out: list[Topic] = []
    for t in topics:
        purposes = tuple(
            Purpose(
                num=p.num,
                name=p.name,
                tag=p.tag,
                leaves=tuple(
                    Leaf(kind=l.kind, text=l.text, tag=l.tag,
                         src=l.src, nbytes=l.nbytes, sha16=l.sha16)
                    for l in p.leaves
                ),
            )
            for p in t.purposes
        )
        out.append(Topic(num=t.num, name=t.name, tag=t.tag, purposes=purposes))
    return out


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_delta(lines: Sequence[str], *, start_line: int = 1) -> tuple[DeltaOp, ...]:
    """把增量行解析成 ``DeltaOp`` 元组。非法 → ``AttributionError``（带行号）。

    空行与 ``#`` / ``//`` 注释行跳过（不计入行号偏移，但行号仍按输入位置报）。
    """
    ops: list[DeltaOp] = []
    for offset, raw in enumerate(lines):
        line_no = start_line + offset
        line = raw.rstrip("\n").rstrip("\r")
        if line.strip() == "":
            continue
        if line.startswith("#") or line.startswith("//"):
            continue
        body = line.strip()
        where = "增量第 %d 行" % line_no

        m = RE_SIGIL.match(body)
        if not m:
            hint = ""
            if RE_LOOKS_LIKE_SIGIL.match(body):
                hint = ("（标记后必须紧跟一个空格：写 `T 品牌定位`，"
                        "不要写 `T1 品牌定位` —— 编号由 SDK 分配，调用方不指定）")
            elif not body:
                hint = "（空行不计入增量）"
            raise AttributionError(
                "增量行无法识别：`%s`%s；行首标记只能是 T（事物）/ P（目的）/ "
                "R（要求）/ E（事件节点）" % (sanitize(body), hint))

        sigil = m.group(1)
        rest = m.group(2) or ""
        text, tag = split_tag(rest)

        if sigil == DeltaKind.T.value:
            if not text:
                raise AttributionError("%s：`T` 行缺少事物名称" % where)
            check_text(text, where)
            check_tag(tag, where)
            ops.append(DeltaOp(kind=DeltaKind.T, text=normalize(text), tag=tag, line_no=line_no))
            continue

        if sigil == DeltaKind.P.value:
            if not text:
                raise AttributionError("%s：`P` 行缺少目的名称" % where)
            check_text(text, where)
            check_tag(tag, where)
            ops.append(DeltaOp(kind=DeltaKind.P, text=normalize(text), tag=tag, line_no=line_no))
            continue

        if sigil == DeltaKind.E.value:
            m_e = RE_EVENT_META.match("%s %s" % (DeltaKind.E.value, text))
            if not m_e:
                raise AttributionError(
                    "%s：增量事件行格式非法，应为 `E 摘要 | src:路径 | bytes:字节数 | "
                    "sha256:%s`（四段齐全、顺序固定、用 ` | ` 分隔）：%s"
                    % (where, "0" * EVENT_SHA_LEN, sanitize(body, 70)))
            summary, src = m_e.group(1), m_e.group(2)
            nbytes, sha = int(m_e.group(3)), m_e.group(4)
            check_summary(summary, where)
            check_src(src, where)
            check_sha16(sha, where)
            check_tag(tag, where)
            if nbytes < 0:
                raise AttributionError("%s：事件行 bytes 不能为负：%d" % (where, nbytes))
            ops.append(DeltaOp(
                kind=DeltaKind.E, text=normalize(summary), tag=tag, line_no=line_no,
                src=src, nbytes=nbytes, sha16=sha,
            ))
            continue

        # R
        if not text:
            raise AttributionError("%s：`R` 行缺少内容" % where)
        check_text(text, where)
        check_tag(tag, where)
        ops.append(DeltaOp(kind=DeltaKind.R, text=normalize(text), tag=tag, line_no=line_no))

    return tuple(ops)


# ---------------------------------------------------------------------------
# 归并
# ---------------------------------------------------------------------------
def _find_topic(topics: Sequence["_MTopic"], name: str) -> "_MTopic | None":
    target = normalize(name)
    for t in topics:
        if normalize(t.name) == target:
            return t
    return None


def _find_purpose(topic: "_MTopic", name: str) -> "_MPurpose | None":
    target = normalize(name)
    for p in topic.purposes:
        if normalize(p.name) == target:
            return p
    return None


def apply_delta(
    topics: Sequence[Topic],
    ops: Sequence[DeltaOp],
    *,
    alloc: NumberAllocator | None = None,
) -> tuple[list[Topic], MergeStat]:
    """把增量并入键树。返回 ``(新 topics, MergeStat)``。确定性、幂等、不改入参。

    ``alloc`` 为空时用「最小未用正整数」（``ctx.py`` 行为）；存储层传入
    ``NumberAllocator`` 以兑现 SCHEMA 的「编号只增不减」。
    """
    tree = to_mutable(topics)
    stat = dict(
        t_new=0, p_new=0, r_new=0, r_dup=0,
        t_retag=0, p_retag=0, r_retag=0,
        e_new=0, e_dup=0, e_upd=0, e_retag=0,
    )
    cur_topic: _MTopic | None = None
    cur_purpose: _MPurpose | None = None

    for op in ops:
        where = "增量第 %d 行" % op.line_no

        if op.kind is DeltaKind.T:
            topic = _find_topic(tree, op.text)
            if topic is None:
                num = alloc.topic() if alloc is not None else _smallest_unused_topic(tree)
                topic = _MTopic(num=num, name=op.text, tag=op.tag)
                tree.append(topic)
                stat["t_new"] += 1
            elif op.tag is not None and topic.tag != op.tag:
                topic.tag = op.tag
                stat["t_retag"] += 1
            cur_topic = topic
            cur_purpose = None
            continue

        if op.kind is DeltaKind.P:
            if cur_topic is None:
                raise AttributionError(
                    "%s：`P` 行之前没有 `T` 行，无法确定它挂在哪个事物下"
                    "（增量必须按 T → P → R 的层级顺序书写）" % where)
            purpose = _find_purpose(cur_topic, op.text)
            if purpose is None:
                if alloc is not None:
                    seq = alloc.purpose(cur_topic.num)
                else:
                    seq = _smallest_unused_purpose(cur_topic)
                purpose = _MPurpose(num="%d.%d" % (cur_topic.num, seq), name=op.text, tag=op.tag)
                cur_topic.purposes.append(purpose)
                stat["p_new"] += 1
            elif op.tag is not None and purpose.tag != op.tag:
                purpose.tag = op.tag
                stat["p_retag"] += 1
            cur_purpose = purpose
            continue

        if op.kind is DeltaKind.E:
            if cur_purpose is None:
                raise AttributionError(
                    "%s：`E` 行之前没有 `P` 行，无法确定它挂在哪个目的下" % where)
            hit: _MLeaf | None = None
            for leaf in cur_purpose.leaves:
                if leaf.kind == "E" and normalize(leaf.text) == op.text and leaf.src == op.src:
                    hit = leaf
                    break
            if hit is None:
                cur_purpose.leaves.append(_MLeaf(
                    kind="E", text=op.text, tag=op.tag,
                    src=op.src, nbytes=op.nbytes, sha16=op.sha16,
                ))
                stat["e_new"] += 1
            elif hit.sha16 != op.sha16 or hit.nbytes != op.nbytes:
                hit.sha16, hit.nbytes = op.sha16, op.nbytes
                stat["e_upd"] += 1
            elif op.tag is not None and hit.tag != op.tag:
                hit.tag = op.tag
                stat["e_retag"] += 1
            else:
                stat["e_dup"] += 1
            continue

        # R
        if cur_purpose is None:
            raise AttributionError(
                "%s：`R` 行之前没有 `P` 行，无法确定它挂在哪个目的下" % where)
        hit_r: _MLeaf | None = None
        for leaf in cur_purpose.leaves:
            if leaf.kind == "R" and normalize(leaf.text) == op.text:
                hit_r = leaf
                break
        if hit_r is not None:
            # 同文本 + 新标记 = 改标记；无标记 = 不动标记（不是清空）
            if op.tag is not None and hit_r.tag != op.tag:
                hit_r.tag = op.tag
                stat["r_retag"] += 1
            else:
                stat["r_dup"] += 1
            continue
        cur_purpose.leaves.append(_MLeaf(kind="R", text=op.text, tag=op.tag))
        stat["r_new"] += 1

    changed = any(stat[k] for k in (
        "t_new", "p_new", "r_new", "t_retag", "p_retag", "r_retag",
        "e_new", "e_upd", "e_retag",
    ))
    return to_frozen(tree), MergeStat(changed=changed, **stat)


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------
def body_key(snapshot: "StoreSnapshot") -> str:
    """内容指纹（不含 rev）。``rev`` 是它的函数，不是操作计数的函数（P4）。"""
    return _content_fingerprint(snapshot.top_text, snapshot.scope, snapshot.topics)


__all__ = [
    "NumberAllocator",
    "RE_EVENT_META",
    "apply_delta",
    "body_key",
    "parse_delta",
    "to_frozen",
    "to_mutable",
]
