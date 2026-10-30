"""归一化 / 校验 / 指纹。

三件事，全部是「口径」而不是「逻辑」，因此集中在一个文件里，谁要改口径都只改这里：

1. **归一化** ``normalize()``：只做空白折叠，**不做 Unicode 归一化**
   （继承 ``ctx.py`` 取舍 #10）。否则同一份存储在 NFC/NFD 环境下会判出不同的键，
   确定性从根上崩掉。
2. **校验** ``check_*()``：每条规则给中文定位（含字段名与长度实例），
   延续 ``ctx.py`` 的 ``_check_*`` 风格。未知叶子种类**立即报错**（I8）。
3. **指纹** ``body_key`` / ``sha16`` / ``snapshot_id``：确定性、可复现。

指纹与 ``ctx.py`` ``_body_key`` 的三处**故意差异**（SCHEMA §4.2）：

* 分隔符从 ``|`` 换成 ``\\x1f``（字段）/ ``\\x1e``（记录）。标记是自由文本，
  用 ``|`` 拼会让 ``tag="a|b"`` 与「文本 a、标记 b」撞成同一串 —— 指纹碰撞
  会让该递增 ``rev`` 时不递增，no-op 判定从根上失效。
* 无标记记为 ``""`` 而不是 Python 的 ``None``（后者 ``"%s"`` 出字符串 ``"None"``，
  与「标记字面为 None」同串）。
* ``scope`` 参与指纹（沿用原设计：作用域变了就是内容真的变了）。

依赖：``errors``（L0 叶子）+ ``model``（仅类型引用）。**不 import 存储/时钟/环境。**
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Sequence

from .errors import AttributionError, EncodingError_, InvariantError

if TYPE_CHECKING:
    from .model import AssemblyPlan, Leaf, Purpose, Topic

# ---------------------------------------------------------------------------
# 常量（与 ``references/store-format.md`` 逐字对齐）
# ---------------------------------------------------------------------------
FMT_SCHEMA = 1
FORMAT_VERSION = "1"

SIGIL_TOPIC = "T"
SIGIL_PURPOSE = "P"
SIGIL_REQUEST = "R"
SIGIL_EVENT = "E"

MAX_NAME = 200
MAX_TOP = 500
MAX_TAG = 12
MAX_SUMMARY = 200
MAX_SRC = 400
EVENT_SHA_LEN = 16
FILE_SHA_LEN = 16          # PayloadBlock.sha16 / snapshot_id 的截断长度

LEAF_KINDS = ("R", "E")

SCOPE_KINDS = ("user", "proj", "task")
MAX_SCOPE = 64
RE_SCOPE = re.compile(r"^(?:user|proj|task)(?:[:/][A-Za-z0-9_.\-]{1,48})?$")

# 生命周期 / 策略标记（自由文本亦可，这几个词被 freeze / prune 特判）
TAG_PINNED = "钉住"
TAG_LONG = "长期"
TAG_TRANSIENT = "暂态"
TAG_CONSTRAINT = "约束"
TAG_STALE = "已废"
TAG_LATEST = "最新"
TAG_TODO = "待办"
TAG_FILL = "待补"

LIFECYCLE_TAGS = (TAG_PINNED, TAG_LONG, TAG_TRANSIENT)

# 控制字符：V11 的完整集合在应用层（SQLite 的 CHECK 兜不住字符集，SCHEMA §9.6）
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
RE_TAG = re.compile(r" \[([^\[\]]+)\]$")

# 指纹分隔符：字段 \x1f，记录 \x1e。控制字符在名称/标记/摘要/src 里全被
# CTRL_RE 禁掉，所以这两个字符不可能出现在字段值里 —— 这是它们能当分隔符的前提。
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """连续空白折叠成一个空格 + 首尾去空白。**不做 Unicode 归一化。**"""
    return re.sub(r"\s+", " ", text).strip()


def fold_ascii(text: str) -> str:
    """只折 ASCII 大小写。

    刻意不用 ``str.lower()``：它会做 Unicode 大小写折叠（土耳其语 dotless i、
    ``ß`` → ``ss`` 等），而折叠规则随 Unicode 版本变 —— 那就把「同一输入同一结果」
    绑死在了 Python 版本上（§11.4 禁止 locale 相关折叠）。
    """
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(ord(ch) + 32))
        else:
            out.append(ch)
    return "".join(out)


def fold_query(text: str) -> str:
    """相关度选取的查询侧归一化：折叠空白 + ASCII 大小写折叠。"""
    return fold_ascii(normalize(text))


# ---------------------------------------------------------------------------
# 标记拆分
# ---------------------------------------------------------------------------
def split_tag(rest: str) -> tuple[str, str | None]:
    """把行尾 `` [xxx]`` 拆成 (正文, 标记或 None)。正文本身不动。

    与 ``ctx.py._split_tag`` 逐字一致：只剥掉标记前的一个空格并 ``rstrip()``。
    """
    m = RE_TAG.search(rest)
    if m:
        return rest[: m.start()].rstrip(), m.group(1)
    return rest, None


# ---------------------------------------------------------------------------
# 净化（错误信息 / 降级 detail 必用）
# ---------------------------------------------------------------------------
def sanitize(text: str, limit: int = 200) -> str:
    """把不可信文本净化成单行可安全打印的形式。继承 ``ctx.py._show``。"""
    out = []
    for ch in text:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < " " or ch == "\x7f":
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    s = "".join(out)
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def check_text(text: str, where: str, limit: int = MAX_NAME) -> None:
    """名称类文本的公共校验（V10 / V11 / V12 的合并版）。"""
    if not text:
        raise AttributionError("%s：名称为空" % where)
    if CTRL_RE.search(text):
        raise AttributionError("%s：名称含控制字符（制表符请改用空格）：%s"
                              % (where, sanitize(text, 40)))
    if "\ufffd" in text:
        raise AttributionError("%s：名称含替换字符 U+FFFD，源文本编码已损坏：%s"
                              % (where, sanitize(text, 40)))
    if "[" in text or "]" in text:
        raise AttributionError(
            "%s：名称含半角方括号，与行尾标记语法冲突（如需方括号请改用全角［］）：%s"
            % (where, sanitize(text, 40)))
    if len(text) > limit:
        raise AttributionError("%s：名称长度 %d 超过上限 %d" % (where, len(text), limit))


def check_top(text: str, where: str) -> None:
    check_text(text, where, limit=MAX_TOP)


def check_tag(tag: str | None, where: str) -> None:
    if tag is None:
        return
    if not tag:
        raise AttributionError("%s：行尾标记为空（` []`）；不需要标记就不要写方括号" % where)
    if CTRL_RE.search(tag):
        raise AttributionError("%s：标记含控制字符：%s" % (where, sanitize(tag, 20)))
    if "[" in tag or "]" in tag:
        raise AttributionError("%s：标记内含方括号：%s" % (where, sanitize(tag, 20)))
    if len(tag) > MAX_TAG:
        raise AttributionError("%s：行尾标记长度 %d 超过上限 %d（` [%s...]`）"
                              % (where, len(tag), MAX_TAG, sanitize(tag, 6)))


def check_summary(text: str, where: str) -> None:
    """E 行摘要：与 R 行同样禁方括号，另外**禁半角竖线**（V18）。"""
    check_text(text, where, limit=MAX_SUMMARY)
    if not normalize(text):
        raise AttributionError(
            "%s：事件摘要只有空白，等于没有摘要（写一个能认出这块素材的名字）" % where)
    if "|" in text:
        raise AttributionError(
            "%s：事件摘要不能含半角竖线 `|`（它与 ` | src:` 分隔符冲突；需要竖线请改用全角｜）：%s"
            % (where, sanitize(text, 40)))


def check_src(text: str | None, where: str) -> None:
    if not text:
        raise AttributionError("%s：事件行的 src 路径为空" % where)
    if CTRL_RE.search(text):
        raise AttributionError("%s：事件行的 src 路径含控制字符：%s" % (where, sanitize(text, 60)))
    if len(text) > MAX_SRC:
        raise AttributionError("%s：事件行的 src 路径长度 %d 超过上限 %d"
                              % (where, len(text), MAX_SRC))


def check_sha16(value: str | None, where: str) -> None:
    if value is None or not re.fullmatch(r"[0-9a-f]{%d}" % EVENT_SHA_LEN, value):
        raise AttributionError(
            "%s：事件行的 sha256 指纹必须是 %d 位小写十六进制，实测 %s"
            % (where, EVENT_SHA_LEN, sanitize(str(value), 24)))


def check_scope(scope: str | None, where: str) -> None:
    """作用域必须是 user / proj / task，可带限定名（proj:demo / task:2026-10-15-x）。"""
    if scope is None:
        return
    if CTRL_RE.search(scope) or "\ufffd" in scope:
        raise AttributionError("%s：作用域含控制字符或替换字符" % where)
    if len(scope) > MAX_SCOPE:
        raise AttributionError("%s：作用域长度 %d 超过上限 %d" % (where, len(scope), MAX_SCOPE))
    if not RE_SCOPE.match(scope):
        raise AttributionError(
            "%s：作用域 %r 非法；必须是 %s 之一，或加限定名（如 proj:demo / task:2026-10-15-brand），"
            "限定名只允许字母数字与 _ . -"
            % (where, sanitize(scope, 24), " / ".join(SCOPE_KINDS)))


def check_leaf_kind(kind: str) -> str:
    """叶子种类白名单。**未知种类立即报错，绝不默认当 R**（I8）。"""
    if kind not in LEAF_KINDS:
        raise InvariantError(
            "内部错误：未知的叶子种类 %r（只支持 %s）。"
            "这是代码缺陷而非数据问题 —— 新增行类型时必须同步所有分派点。"
            % (kind, " / ".join(LEAF_KINDS)))
    return kind


def scope_kind(scope: str | None) -> str | None:
    """取作用域的种类部分（proj:demo -> proj）；未声明返回 None。"""
    if not scope:
        return None
    return re.split(r"[:/]", scope, maxsplit=1)[0]


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------
def is_encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def ensure_utf8(text: str, where: str) -> tuple[str, bool]:
    """保证可 utf-8 编码。不可编码时用 ``backslashreplace`` 转义。

    返回 ``(可编码文本, 是否发生了转义)``。**绝不** 用 ``errors="replace"``：
    那会产出 U+FFFD 污染，而 U+FFFD 本身是 V11 的违禁字符，等于制造事后
    查不出来的数据损坏（``references/degradation.md`` D7/D20）。
    """
    if is_encodable(text):
        return text, False
    try:
        escaped = text.encode("utf-8", "backslashreplace").decode("utf-8")
    except UnicodeError as exc:  # pragma: no cover - backslashreplace 不应再失败
        raise EncodingError_("%s：文本无法转义为 UTF-8：%s" % (where, exc)) from exc
    return escaped, True


def encode_or_raise(text: str, where: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EncodingError_(
            "%s：文本不是合法 UTF-8（位置 %d 附近，字符 %r 是孤立代理项）"
            % (where, exc.start, exc.object[exc.start:exc.end])) from exc


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha16_text(text: str) -> str:
    """``sha256(text.encode("utf-8"))[:16]`` —— PayloadBlock / SystemPrompt 用。"""
    return hashlib.sha256(encode_or_raise(text, "指纹计算")).hexdigest()[:FILE_SHA_LEN]


def sha16_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:FILE_SHA_LEN]


def body_key(
    top_text: str | None,
    scope: str | None,
    topics: Sequence["Topic"],
) -> str:
    """内容指纹前像（不含 rev）。P4：``rev`` 只在它真变化时 +1。

    形态严格照 SCHEMA §4.2：记录用 ``\\x1e`` 分隔、字段用 ``\\x1f`` 分隔，
    末尾补 ``\\x1e``。遍历顺序恒为「topic 序 → purpose 序 → 叶子序」，
    不依赖任何哈希/字典序（P5）。
    """
    parts: list[str] = ["ctx1"]
    parts.append("top=" + (top_text or ""))
    parts.append("scope=" + (scope or ""))
    for t in topics:
        parts.append(FIELD_SEP.join(("T", str(t.num), t.name, t.tag or "")))
        for p in t.purposes:
            parts.append(FIELD_SEP.join(("P", str(t.num), p.num, p.name, p.tag or "")))
            for leaf in p.leaves:
                kind = check_leaf_kind(leaf.kind)
                if kind == "E":
                    parts.append(FIELD_SEP.join((
                        "E", leaf.text, leaf.src or "", str(leaf.nbytes or 0),
                        leaf.sha16 or "", leaf.tag or "",
                    )))
                else:
                    parts.append(FIELD_SEP.join(("R", leaf.text, leaf.tag or "")))
    return "".join(p + RECORD_SEP for p in parts)


def body_hash(top_text: str | None, scope: str | None, topics: Sequence["Topic"]) -> str:
    """64 位小写十六进制（``store.body_hash`` 的 CHECK 要求）。"""
    return sha256_hex(body_key(top_text, scope, topics).encode("utf-8"))


def plan_key(plan: "AssemblyPlan | None") -> str:
    """``AssemblyPlan`` 的规范化串（审计/日志用，**不进** ``snapshot_id``）。"""
    if plan is None:
        return "plan=none"
    return FIELD_SEP.join((
        "plan", str(plan.schema), str(plan.generation), str(plan.epoch_turn),
        str(plan.r_first_turn), plan.frozen_sha16, plan.system_sha16,
        plan.snapshot_id, str(plan.store_rev), plan.counter_id, plan.policy_digest,
    ))


def snapshot_id(*, schema: int, system_sha16: str, frozen_sha16: str) -> str:
    """P6：``snapshot_id = sha256("ctxsnap1" \\x1f schema \\x1f system_sha16 \\x1f frozen_sha16)[:16]``。

    它只回答一个问题：「决定 S+F 字节的那份内容，还是不是同一份」。
    所以输入只有两个**权威哈希**（外加 schema 号），不另算 dump。

    **刻意不含**：键树、``rev``、``generation``、turn 边界、rowid、时间戳、路径、
    ``policy_digest``。前三项每一项都是一条已知的静默失效路径：

    * 含键树 → 每 ``attribute()`` 一次它就变 → 每轮判 ``PLAN_INVALID`` → 每轮重压 →
      每轮全价轮（A7 实例 3）；
    * 含 ``rev`` / turn 边界 → 同上，只是更快；
    * 含 ``generation`` / 时间戳 → 同一份 F 的 id 不再稳定，"这份 F 是不是同一份"答不出来。

    「派生量不可能与它的源打架」是选派生而非另算 dump 的全部理由：结构上不存在
    两个哈希不一致这件事，因此也就没有要维护的一致性。
    """
    pre = FIELD_SEP.join(("ctxsnap1", str(int(schema)), system_sha16, frozen_sha16))
    return sha256_hex(pre.encode("utf-8"))[:FILE_SHA_LEN]


__all__ = [
    "CTRL_RE",
    "EVENT_SHA_LEN",
    "FIELD_SEP",
    "FMT_SCHEMA",
    "FORMAT_VERSION",
    "LEAF_KINDS",
    "LIFECYCLE_TAGS",
    "MAX_NAME",
    "MAX_SCOPE",
    "MAX_SRC",
    "MAX_SUMMARY",
    "MAX_TAG",
    "MAX_TOP",
    "RECORD_SEP",
    "RE_SCOPE",
    "RE_TAG",
    "SCOPE_KINDS",
    "SIGIL_EVENT",
    "SIGIL_PURPOSE",
    "SIGIL_REQUEST",
    "SIGIL_TOPIC",
    "TAG_CONSTRAINT",
    "TAG_FILL",
    "TAG_LATEST",
    "TAG_LONG",
    "TAG_PINNED",
    "TAG_STALE",
    "TAG_TODO",
    "TAG_TRANSIENT",
    "body_hash",
    "body_key",
    "check_leaf_kind",
    "check_scope",
    "check_sha16",
    "check_src",
    "check_summary",
    "check_tag",
    "check_text",
    "check_top",
    "encode_or_raise",
    "ensure_utf8",
    "fold_ascii",
    "fold_query",
    "is_encodable",
    "normalize",
    "plan_key",
    "sanitize",
    "scope_kind",
    "sha16_bytes",
    "sha16_text",
    "sha256_hex",
    "snapshot_id",
    "split_tag",
]
