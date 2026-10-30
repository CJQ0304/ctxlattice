"""不可变数据模型。

全部 ``frozen=True, slots=True`` —— 不可变是 A3（装配是纯函数）的第一道保险：
任何「顺手改一下」都会在赋值处炸，而不是在 20 轮后的 ``cached_i`` 归零处炸。

依赖：``errors`` 之前的叶子层；不 import 任何内部模块。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Protocol

if TYPE_CHECKING:  # 仅类型引用，避免把 text / policy 拖进 import 图
    from .policy import BoundaryReason, DegradeReason


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------
class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # 对齐 TRANSCRIPT-FORMAT：role ∈ {user, assistant, tool}


class BlockKind(str, Enum):
    S = "S"  # 静态 system
    F = "F"  # 冻结记忆块
    R = "R"  # 原文轮段（边界内只追加）
    U = "U"  # 本轮用户消息


class Framing(str, Enum):
    SINGLE_SYSTEM = "single_system"  # S+F 合并为 1 条 system；R 逐条；U 收尾。默认
    SYSTEM_ONLY = "system_only"      # 只有 S 进 system；F 作为首条 user
    RAW = "raw"                      # 不映射，交给自定义适配器


class FrozenView(Protocol):
    """装配侧对 F 的**全部**要求（§4.1 / §4.9）。

    为什么定义在 ``model`` 而不是 ``assembler``：这是**生产者与消费者共享的契约**。
    F 的三个字段由 Session 从 ``plan`` 的三列原样取出（见 ``freeze.frozen_from_plan``），
    装配器只是消费它。若把协议放装配器里，等于让消费者拥有生产者的接口 ——
    下一个要产出 F 的地方（replay 工具、bench 的两臂替身）就得反过来 import
    ``assembler``，而那正是 A3 要避免的方向。

    为什么它是 Protocol 而不是直接标 ``freeze.FrozenBlock``：那条注解会**逼**
    ``assembler`` import ``freeze``（文档原样照做就必然长出这个 import），
    而 ``freeze`` 会连带把 ``relevance`` 和检索入口拖进装配侧的 import 闭包。
    改用结构子类型之后，``assembler → freeze`` 这条边不是"被禁止"，而是**不必要**：
    禁止靠纪律，不必要靠结构。

    ⚠️ **只声明一个字段，是刻意的。** 装配器读 F 只做一件事：
    ``make_block(F, frozen.text, counter.count(frozen.text))`` —— 它不碰哈希、
    不碰 F 自己的 token 计数。

    多声明 ``sha16`` / ``token_count`` 的代价不是"多两个属性"：那等于**邀请**
    装配器拿它们做判断（"哈希对不上就重建""token 数超了就裁"），而这两件事
    都属于边界的事，一旦挪进装配期，F 就会在边界之间变化 —— 前缀每轮断裂、
    缓存全废、**且不报错**。I3 要防的就是这个。

    反过来说，窄协议还顺手消掉了一个失败模式：三字段版必须与 ``freeze.FrozenBlock``
    逐字对齐（``token_count`` 写成 ``tokens`` 也行、运行期完全正常，只有 mypy 会报）。
    只留 ``text`` 之后，没有可对不齐的东西 —— 结构子类型允许提供方多带属性，
    ``FrozenBlock`` 天然满足。
    """

    text: str


# blocks 的固定顺序（I4）。顺序即字节顺序，不可运行时调整。
# 用模块级常量而不是枚举成员：「顺序」是装配器里的一个循环，不需要可迭代枚举。
BLOCK_ORDER: tuple[BlockKind, ...] = (BlockKind.S, BlockKind.F, BlockKind.R, BlockKind.U)


# ---------------------------------------------------------------------------
# 轮次
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮 = 1 条 user + 其后若干条 assistant/tool。

    口径见 TRANSCRIPT-FORMAT：「一轮 ``user`` 之后的所有行属于同一轮」。
    """

    index: int                                # 1 起，连续，不得跳号
    user_text: str
    replies: tuple[Message, ...] = ()          # 可为空（本轮回复尚未产生）
    pinned: bool = False                       # transcript 的 pinned:true → 参与 T1

    def messages(self) -> tuple[Message, ...]:
        """user + replies，供 R 段渲染。顺序即字节顺序。"""
        return (Message(Role.USER, self.user_text),) + self.replies

    def with_reply(self, message: Message) -> "Turn":
        """追加一条回复，返回新 Turn（不可变，不就地改）。"""
        return Turn(
            index=self.index,
            user_text=self.user_text,
            replies=self.replies + (message,),
            pinned=self.pinned,
        )

    def with_pinned(self, pinned: bool) -> "Turn":
        return Turn(self.index, self.user_text, self.replies, pinned)


# ---------------------------------------------------------------------------
# 键树
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Leaf:
    kind: str                     # "R" | "E"（白名单，未知即报错——继承 _kind()）
    text: str
    tag: str | None
    src: str | None = None        # 仅 E
    nbytes: int | None = None     # 仅 E
    sha16: str | None = None      # 仅 E

    def __post_init__(self) -> None:
        # 结构校验不在这里做（那是 text.py 的职责，且错误要带行号），
        # 这里只保证「E 的指针三元组齐全 / R 全空」这条不可变不变量成立。
        if self.kind == "E":
            if self.src is None or self.nbytes is None or self.sha16 is None:
                raise ValueError("E 叶子必须同时给出 src / nbytes / sha16")
        elif self.kind == "R":
            if self.src is not None or self.nbytes is not None or self.sha16 is not None:
                raise ValueError("R 叶子不得携带 src / nbytes / sha16")
        # 未知 kind 交给 text.check_leaf 报（那里能给出中文定位）。


@dataclass(frozen=True, slots=True)
class Purpose:
    num: str                      # "1.3"
    name: str
    tag: str | None
    leaves: tuple[Leaf, ...]

    def path(self, topic_num: int) -> str:
        return "T%d/P%s" % (topic_num, self.num)


@dataclass(frozen=True, slots=True)
class Topic:
    num: int
    name: str
    tag: str | None
    purposes: tuple[Purpose, ...]

    @property
    def path(self) -> str:
        return "T%d" % self.num


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    text: str
    sha16: str
    source: str                   # "builtin:prompts/system.md" | "caller"


# ---------------------------------------------------------------------------
# 装配计划 / 快照
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AssemblyPlan:
    """字节稳定性的持久锚点，且**自包含**。

    payload 字节完全由 ``(system.text, frozen_text, turns[r_first:], user_text)`` 决定。

    ``frozen_text`` 是**落库的 F 正文**，不是装配期重算的派生物（I6 / P8 / §5.4）：
    装配只读它，一个字符都不动。``frozen_sha16`` 在读时校验（不符即
    ``StoreCorruptError`` → ``PLAN_INVALID`` 重建），``frozen_tokens`` 是冻结时的
    口径快照，供报告复算。

    ``snapshot_id`` 与 ``store_rev`` 是**审计字段**：只写不判（A7）。
    拿它们比当前值当有效性闸门，会让每轮都判失效 → 每轮重压 → 每轮全价轮，
    而且不报错。
    """

    schema: int                   # = 1
    generation: int               # 每次压缩 +1
    epoch_turn: int               # 本次 F 冻结所在的轮次
    r_first_turn: int             # R 段起始轮（含）——**段起点，不是滑动窗口宽度**
    frozen_text: str              # ★ F 的逐字内容（装配期直接使用，绝不重算）
    frozen_sha16: str             # = sha16(frozen_text)
    frozen_tokens: int            # 冻结时的 token 数（按 counter_id）
    system_sha16: str
    snapshot_id: str              # 由 schema + system_sha16 + frozen_sha16 派生（审计用）
    store_rev: int                # 写本行时的 store.rev（审计用，只写不判）
    counter_id: str
    policy_digest: str


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """一致只读视图（P1）。``snapshot_id`` 必须可复现（P6）。

    与 ARCHITECTURE §4.1 的差异（**只有追加，没有改名/改序**）：
    ``top_text`` / ``scope`` 以带默认值的形式追加在末尾。原因是 T0（会话总目标）
    与 SCHEMA §4.2 的指纹前像（``top=`` / ``scope=``）都需要它们，而原文的字段表
    没有任何入口能拿到 —— 缺了它们 T0 与 ``body_hash`` 都无法实现。
    """

    snapshot_id: str
    rev: int
    topics: tuple[Topic, ...]
    turn_lo: int
    turn_hi: int
    plan: AssemblyPlan | None
    system: SystemPrompt | None
    event_count: int
    top_text: str | None = None
    scope: str | None = None


# ---------------------------------------------------------------------------
# 归并统计 / 事件记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MergeStat:
    t_new: int = 0
    p_new: int = 0
    r_new: int = 0
    r_dup: int = 0
    t_retag: int = 0
    p_retag: int = 0
    r_retag: int = 0
    e_new: int = 0
    e_dup: int = 0
    e_upd: int = 0
    e_retag: int = 0
    changed: bool = False         # False = 内容指纹未变 → 不写盘、不 +rev

    def as_notes(self) -> tuple[str, ...]:
        """给调用方/日志的可读摘要（中文，不含任何 payload 相关字节）。"""
        parts = []
        for label, value in (
            ("新增事物", self.t_new),
            ("新增目的", self.p_new),
            ("新增要求", self.r_new),
            ("重复要求", self.r_dup),
            ("改标记(事物)", self.t_retag),
            ("改标记(目的)", self.p_retag),
            ("改标记(要求)", self.r_retag),
            ("新增事件", self.e_new),
            ("重复事件", self.e_dup),
            ("事件指纹更新", self.e_upd),
            ("改标记(事件)", self.e_retag),
        ):
            if value:
                parts.append("%s %d" % (label, value))
        if not parts:
            parts.append("无变化")
        if not self.changed:
            parts.append("内容指纹未变，未写盘")
        return tuple(parts)


class DeltaKind(str, Enum):
    """增量行的行首标记（白名单，未知即报错）。"""

    T = "T"
    P = "P"
    R = "R"
    E = "E"


@dataclass(frozen=True, slots=True)
class DeltaOp:
    """一条已解析、已校验的增量行。

    ``line_no`` 保留原始行号，错误文案与降级记录都靠它定位。
    """

    kind: DeltaKind
    text: str                     # 已归一的名称/摘要/内容（_norm 之后）
    tag: str | None
    line_no: int
    src: str | None = None
    nbytes: int | None = None
    sha16: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionEvent:
    generation: int
    at_turn: int
    reason: "BoundaryReason"
    included: Mapping[str, int]   # tier → 纳入条数（T0..T7）
    omitted: Mapping[str, int]    # tier → 因预算丢弃条数（诚实记账）
    frozen_sha16: str
    frozen_tokens: int
    aborted: bool = False
    abort_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DegradationRecord:

    at_turn: int
    at_tick: int                  # 逻辑时钟 tick，非墙钟
    reason: "DegradeReason"
    detail: str                   # 已净化（控制字符转义），≤200 字符
    snapshot_id: str | None
    fallback: str                 # "full_history" | "buffer_only" | "last_resort"

@dataclass(frozen=True, slots=True)
class Measure:
    """一个测点（STORE-SCHEMA 第 11 表 ``measure``）。

    这是 bench 的记账单元，**不参与装配**：它的任何字段都不进 payload 字节。
    ``cached_tokens <= input_tokens`` 由存储层 DDL 兜住 —— 把缓存命中既当
    「已发」又当「另算」是这一层最常见的一类错，口径一错，后面所有比例都错。
    """

    session_id: str
    turn_no: int
    attempt: int = 1
    input_tokens: int = 0        # 实际发出的 gross input
    cached_tokens: int = 0       # 与上一轮 payload 的公共前缀 token 数
    output_tokens: int = 0
    payload_hash: str | None = None   # 整包 sha256（审计：两臂差异只在历史段）
    system_hash: str | None = None    # S 段 sha256（审计：两臂逐字节相同）
    latency_ms: int | None = None
    ok: bool = True              # 失败也要落一行：否则得到幸存者偏差


@dataclass(frozen=True, slots=True)
class SessionStats:
    turns: int
    boundaries: int
    degradations: int
    store_writable: bool
    last_payload_tokens: int
    peak_payload_tokens: int


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PayloadBlock:
    kind: BlockKind
    text: str                     # 逐字节内容（已含标题/分隔符）
    token_count: int              # 按 counter_id 计的 token 数
    sha16: str                    # sha256(text.encode("utf-8"))[:16]


@dataclass(frozen=True, slots=True)
class Payload:
    """provider 可直接发送的一轮输入。

    ``payload 字节 = 按 blocks 顺序拼接，无额外分隔``（无分隔符是刻意的：
    任何「拼接时插一个 \n」的便利都会让前缀稳定性依赖调用点的数量）。
    """

    blocks: tuple[PayloadBlock, ...]
    counter_id: str
    degraded: bool = False
    degrade_reason: str | None = None
    boundary: bool = False
    cold_start: bool = False

    # -- 派生属性 ---------------------------------------------------------
    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks)

    @property
    def sha16(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()[:16]

    @property
    def token_count(self) -> int:
        return sum(b.token_count for b in self.blocks)

    def block(self, kind: BlockKind) -> PayloadBlock | None:
        for b in self.blocks:
            if b.kind == kind:
                return b
        return None

    def to_bytes(self) -> bytes:
        return self.text.encode("utf-8")

    def kinds(self) -> tuple[BlockKind, ...]:
        return tuple(b.kind for b in self.blocks)

    def to_messages(self, framing: Framing = Framing.SINGLE_SYSTEM) -> tuple[dict[str, str], ...]:
        """映射成 provider 的 messages 数组。

        映射是**便利层**，权威永远是 ``to_bytes()``。R/U 段用 ``user: `` /
        ``assistant: `` / ``tool: `` 行首标记切分，因此文本中若出现以这些前缀
        开头的行会被切错 —— 需要逐字节保真时用 ``Framing.RAW`` 或直接发
        ``to_bytes()``。这条限制写在这里而不是藏起来，因为它不可通过编码规避
        （任何"安全分隔符"都可能出现在用户文本里）。
        """
        if framing is Framing.RAW:
            return ({"role": "user", "content": self.text},)

        s = self.block(BlockKind.S)
        f = self.block(BlockKind.F)
        r = self.block(BlockKind.R)
        u = self.block(BlockKind.U)

        out: list[dict[str, str]] = []
        if framing is Framing.SINGLE_SYSTEM:
            system_text = (s.text if s else "") + (f.text if f else "")
            if system_text:
                out.append({"role": "system", "content": system_text})
        else:  # SYSTEM_ONLY
            if s is not None:
                out.append({"role": "system", "content": s.text})
            if f is not None and f.text:
                out.append({"role": "user", "content": f.text})

        if r is not None:
            for role, content in _split_role_lines(r.text):
                out.append({"role": role, "content": content})
        if u is not None:
            for role, content in _split_role_lines(u.text):
                out.append({"role": role, "content": content})
        return tuple(out)


_ROLE_PREFIXES = ("user: ", "assistant: ", "tool: ")


def _split_role_lines(text: str) -> tuple[tuple[str, str], ...]:
    """把 ``role: text`` 逐行流切成 (role, content)。

    以行首标记为界：一行匹配即开始一条新消息，续行并入当前消息。
    空内容（只有标记）也产出，不丢消息边界。
    """
    out: list[tuple[str, str]] = []
    cur_role: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur_role is not None:
            out.append((cur_role, "\n".join(buf)))

    for line in text.splitlines():
        matched = None
        for prefix in _ROLE_PREFIXES:
            if line.startswith(prefix):
                matched = (prefix[:-2], line[len(prefix):])
                break
        if matched is not None:
            flush()
            cur_role, first = matched
            buf = [first]
        elif cur_role is not None:
            buf.append(line)
        # 没有 role 前缀且还没开始任何消息 → 丢弃（那是块标题行，例如 "[R] ..."）
    flush()
    return tuple(out)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sha16_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_block(kind: BlockKind, text: str, token_count: int) -> PayloadBlock:
    return PayloadBlock(kind=kind, text=text, token_count=token_count, sha16=sha16_of(text))


# ---------------------------------------------------------------------------
# 装配期"零存储读"的运行期计数器（I6 的第三级保证）
# ---------------------------------------------------------------------------
#: 为什么需要它（顾图南 §4.9）：``FrozenView`` 只挡住 ``assembler → freeze``
#: 这条 **import 边**。但 Protocol 是结构协议，下面这个实现它完全合法地接受：
#:
#:     @property
#:     def text(self): return build_frozen_block(...)   # 访问时才重算
#:
#: import 图干净、类型检查通过、I6（F 的字节来自 plan）依然被破 —— 而且安静。
#: 结构保证分三级：**import 图 → 类型 → 运行期断言**；这是第三级。
#:
#: 计数器放在 ``model`` 而不是新开模块：``assembler`` 已经 import 它，
#: 于是断言不需要新增任何一条依赖边（否则"为了证明装配不碰存储"而给装配
#: 加一条依赖，本身就很讽刺）。
_READS = 0


def bump_store_reads(count: int = 1) -> None:
    """存储层的每个读入口各调一次（``snapshot`` / ``read_turns_with_hashes``）。

    端口上只有这两条读路径（``read_turns`` 是组合入口的投影，不单独计数），
    所以"一次会话轮次读了多少次库"是可枚举的。
    """
    global _READS
    _READS += count


def store_read_count() -> int:
    """累计读取次数。只用于"装配前后差值必须为 0"这一个比较，不承载任何业务含义。"""
    return _READS


__all__ = [
    "AssemblyPlan",
    "BLOCK_ORDER",
    "BlockKind",
    "CompactionEvent",
    "DegradationRecord",
    "DeltaKind",
    "DeltaOp",
    "Framing",
    "bump_store_reads",
    "store_read_count",
    "FrozenView",
    "Leaf",
    "MergeStat",
    "Measure",
    "Message",
    "Payload",
    "PayloadBlock",
    "Purpose",
    "Role",
    "SessionStats",
    "StoreSnapshot",
    "SystemPrompt",
    "Topic",
    "Turn",
    "make_block",
    "sha16_of",
    "sha256_hex",
]
