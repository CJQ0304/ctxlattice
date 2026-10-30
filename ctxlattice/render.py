"""逐字节稳定的渲染器 —— **全项目唯一「生成字节」的地方**。

（``assembler.py`` 只做拼接，不生成新的文本片段；它调用的所有片段都来自这里。）

渲染硬约束（继承 ``.ctx`` 的确定性纪律，§4.6）：

* 行尾统一 ``\\n``，无 BOM，无 ``\\r``；
* 不输出任何时间戳、路径（``E`` 的 ``src`` 是存储里的**数据**，不是运行环境信息）、
  平台相关分隔符；
* 名称归一化**只做空白折叠**，不做 Unicode 归一化；
* 叶子种类白名单，未知种类立即抛 ``InvariantError``（继承 ``_kind()``）；
* 任何浮点都不进渲染（分数、比例在相关度选取里就是整数）。

依赖：``model`` / ``text`` / ``errors``。**不 import 存储 / 时钟 / 策略。**
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from .errors import InvariantError
from .model import Message, Role, sha16_of
from .text import (
    SIGIL_EVENT,
    SIGIL_REQUEST,
    check_leaf_kind,
    ensure_utf8,
    normalize,
)

if TYPE_CHECKING:
    from .model import Leaf, Purpose, Topic, Turn

# ---------------------------------------------------------------------------
# 固定常量（改动它们就等于改变所有历史 payload 的字节，需评审）
# ---------------------------------------------------------------------------

#: R 段的块首行。**每一轮都发**，即使 R 段当前一条轮次都没有。
#:
#: 为什么必须无条件存在：R 段在边界轮之后从「零条轮次」开始增长。若块首行
#: 只在有内容时出现，那么第 t 轮（R 空）与第 t+1 轮（R 有一条）之间，payload
#: 会在 U 段之前插入一行 —— 前缀立刻错位，``cached_i`` 退化为 ``count(S)``。
#: 让首行恒定存在，就把「R 段是纯追加」变成结构事实（I1）。
R_HEADER = "[R] 最近原文轮（逐字，边界内只追加）\n"

#: F 段块首行模板。gen / epoch_turn 都来自 plan，边界之间恒定。
F_HEADER_FMT = "[记忆] gen=%d epoch_turn=%d\n"

#: 助手回复缺失时的诚实标记（F10）。**只标记，绝不补造。**
MISSING_REPLY_MARKER = "〔助手回复未记录〕"

#: 编码已在消息体内转义时的行内标记（F6）。
ESCAPED_MARK = "〔含无法编码的字符，已转义〕"

ROLE_LABEL: dict[Role, str] = {
    Role.USER: "user",
    Role.ASSISTANT: "assistant",
    Role.TOOL: "tool",
}


# ---------------------------------------------------------------------------
# system 段
# ---------------------------------------------------------------------------
def normalize_prompt(text: str) -> str:
    """把 S 段文本规范成唯一形态：LF 行尾、行尾无空白、恰好一个结尾换行。

    幂等：``normalize_prompt(normalize_prompt(x)) == normalize_prompt(x)``。
    S 段一旦进入 payload 就**整个会话不再变**（I2），所以它必须没有
    「因为编辑器自作主张加了行尾空格」这类漂移空间。
    """
    if not text:
        raise InvariantError("system 提示文本为空：S 段是 payload 的第 0 个字节，不能为空")
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    if unified.startswith("\ufeff"):
        unified = unified[1:]
    lines = [ln.rstrip() for ln in unified.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise InvariantError("system 提示文本只有空白：请提供真实的 system 段")
    safe, _ = ensure_utf8("\n".join(lines), "system 提示")
    return safe + "\n"


# ---------------------------------------------------------------------------
# 消息 / 轮次（R 段与 U 段共用同一实现 —— 这是 I1 的结构保证）
# ---------------------------------------------------------------------------
def render_message(message: Message) -> str:
    """一条消息渲染成一行起头的文本，**以 ``\\n`` 结尾**。

    U 段 = ``render_message(Message(USER, user_text))``；
    R 段里同一轮的开头也是它。两者是**同一个函数**，所以「上一轮的 U 段是新一轮
    R 段的前缀」不是靠约定，而是编译器/解释器层面的事实。
    """
    label = ROLE_LABEL.get(message.role)
    if label is None:  # pragma: no cover - Role 是封闭枚举
        raise InvariantError("未知的消息角色 %r（只支持 %s）"
                             % (message.role, " / ".join(r.value for r in Role)))
    text, escaped = ensure_utf8(message.text, "%s 消息正文" % label)
    if escaped:
        # 标记放在消息体内、行尾，保住行首的 ``role: `` 前缀（否则切分失效）。
        return "%s: %s %s\n" % (label, text, ESCAPED_MARK)
    return "%s: %s\n" % (label, text)


def render_turn(turn: "Turn", *, missing_marker: str | None = None) -> str:
    """R 段单轮：user 行 + 其后的 assistant/tool 行（+ 缺失标记）。

    ``missing_marker`` 追加在**最后**：它必须位于 U 段（或上一轮的 R 片段）
    之后，才能在前缀关系上"只往后长"。
    """
    out = [render_message(Message(Role.USER, turn.user_text))]
    for reply in turn.replies:
        out.append(render_message(reply))
    if not turn.replies and missing_marker:
        out.append("%s\n" % missing_marker)
    return "".join(out)


def render_turns(turns: Sequence["Turn"], *, missing_marker: str | None = None) -> str:
    return "".join(render_turn(t, missing_marker=missing_marker) for t in turns)


def render_r_segment(turns: Sequence["Turn"], *, missing_marker: str | None = None) -> str:
    """整个 R 段（块首行 + 逐轮原文）。**R 段文本的唯一出口。**

    assembler 拼 payload 用它，compaction 估算「边界后 payload 规模」也用它 ——
    两处若各拼一次，R 段的字节就会有两个出处，而它们各自看起来都很确定。
    """
    return R_HEADER + render_turns(turns, missing_marker=missing_marker)


# ---------------------------------------------------------------------------
# 键树（.ctx 语法，供 F 段引用）
# ---------------------------------------------------------------------------
def render_tag(tag: str | None) -> str:
    return "" if tag is None else " [%s]" % tag


def render_leaf(leaf: "Leaf") -> str:
    """单个叶子成一行（**不带缩进**，缩进由调用方按层级加）。

    ``E`` 行是「摘要 + 指针 + 指纹」，正文永不进 payload（N5 / F14 的故障域收缩）。
    """
    kind = check_leaf_kind(leaf.kind)
    if kind == "E":
        if leaf.src is None or leaf.nbytes is None or leaf.sha16 is None:
            raise InvariantError(
                "E 叶子缺指针三元组：src=%r bytes=%r sha256=%r（摘要 %r）"
                % (leaf.src, leaf.nbytes, leaf.sha16, leaf.text))
        return "%s %s | src:%s | bytes:%d | sha256:%s%s" % (
            SIGIL_EVENT, leaf.text, leaf.src, leaf.nbytes, leaf.sha16, render_tag(leaf.tag))
    return "%s %s%s" % (SIGIL_REQUEST, leaf.text, render_tag(leaf.tag))


def render_purpose(purpose: "Purpose") -> str:
    return "P%s %s%s" % (purpose.num, purpose.name, render_tag(purpose.tag))


def render_topic_line(topic: "Topic") -> str:
    return "T%d %s%s" % (topic.num, topic.name, render_tag(topic.tag))


def render_purpose_line(purpose: "Purpose") -> str:
    """骨架行（缩进 2 格，与 ``.ctx`` 一致）。"""
    return "  " + render_purpose(purpose)


def render_skeleton(topics: Sequence["Topic"]) -> str:
    """只有 T/P 名，无 R —— 导航层（T5）。"""
    out: list[str] = []
    for topic in topics:
        out.append(render_topic_line(topic) + "\n")
        for purpose in topic.purposes:
            out.append(render_purpose_line(purpose) + "\n")
    return "".join(out)


def render_subtree(topic: "Topic", purposes: Sequence["Purpose"]) -> str:
    """T + 指定 P + 其 R/E 行，保持缩进层级与编号（T6）。"""
    out = [render_topic_line(topic) + "\n"]
    for purpose in purposes:
        out.append(render_purpose_line(purpose) + "\n")
        for leaf in purpose.leaves:
            out.append("    " + render_leaf(leaf) + "\n")
    return "".join(out)


def render_event_index(topics: Sequence["Topic"]) -> str:
    """只出 E 行（指针层），T7。"""
    out: list[str] = []
    for topic in topics:
        for purpose in topic.purposes:
            for leaf in purpose.leaves:
                if check_leaf_kind(leaf.kind) == "E":
                    out.append(render_leaf(leaf) + "\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# F 段的行样式
# ---------------------------------------------------------------------------
def tier_line(label: str, body: str) -> str:
    """``[标签] 正文\\n``。F 段所有内容行都走这里，保证前缀形态一致。"""
    return "[%s] %s\n" % (label, body)


def tier_line_indented(label: str, indent: int, body: str) -> str:
    return "[%s] %s%s\n" % (label, " " * indent, body)


def single_line(text: str) -> str:
    """把可能多行的文本压成一行（F 段用：记忆块是摘要层，不是逐字层）。

    R 段逐字原文（I5）**不调用**它。这里之所以要压行：F 段的行首带 ``[标签]``，
    若正文里带换行，就能伪造出一个假的 ``[约束]`` 行，让「哪些是硬约束」
    变成不可判定。
    """
    return normalize(text)


def check_renderable(text: str, where: str) -> str:
    """渲染前的最后一道检查：可 utf-8 编码。"""
    safe, escaped = ensure_utf8(text, where)
    if escaped:
        raise InvariantError("%s：文本含孤立代理项，已转义但仍被要求逐字渲染" % where)
    return safe


def sha16(text: str) -> str:
    return sha16_of(text)


__all__ = [
    "ESCAPED_MARK",
    "F_HEADER_FMT",
    "MISSING_REPLY_MARKER",
    "ROLE_LABEL",
    "R_HEADER",
    "check_renderable",
    "normalize_prompt",
    "render_event_index",
    "render_leaf",
    "render_message",
    "render_purpose",
    "render_purpose_line",
    "render_r_segment",
    "render_skeleton",
    "render_subtree",
    "render_tag",
    "render_topic_line",
    "render_turn",
    "render_turns",
    "sha16",
    "single_line",
    "tier_line",
    "tier_line_indented",
]
