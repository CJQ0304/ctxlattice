"""ctxlattice —— C1 上下文存储 SDK。

公开面：**只 re-export，无逻辑**。任何"顺手在这里加个便利函数"的冲动都应该
被推回到它所属的层 —— 这个文件是依赖图里唯一被允许"知道所有人"的地方，
一旦它有逻辑，它就会成为第二个不受分层约束的入口。

典型用法：

    from ctxlattice import MemoryStore, Policy, Session, make_counter

    policy = Policy()
    store = MemoryStore()
    session = Session.open(store=store, policy=policy)
    payload = session.send("把 render() 里 E 行的缩进改一下")
    provider.send(payload.to_messages())

    session.observe("已定位到 _fmt_item()。")
    session.attribute(["T 渲染器", "P E 行缩进", "R E 行缩进 4 格 [约束]"])

三个必须记住的口径：

* ``send()`` 永不抛（fail-open），``open()`` / ``attribute()`` 按签名抛。
* 非边界轮 payload 以上一轮为**字节前缀**（I1）—— 这不是优化，是方案成立的前提。
* ``payload.boundary=True`` 的轮次缓存必然失效，计费侧必须单独标、不许摊平。
"""

from __future__ import annotations

from .arms import (
    ArmProtocol,
    BaselineArm,
    LatticeArm,
    StepResult,
    assert_arms_aligned,
)
from .assembler import (
    AssemblyInputs,
    assemble,
    assert_prefix_stable,
    common_prefix_bytes,
    describe_payload,
    prefix_violation,
)
from .attribution import NumberAllocator, apply_delta, body_key, parse_delta
from .clock import ClockPort, FrozenClock, LogicalClock, SystemClock
from .compaction import (
    CompactionResult,
    compact,
    describe_event,
    tokens_after_boundary,
)
from .degrade import (
    buffer_only_payload,
    classify,
    full_history_payload,
    last_resort_payload,
)
from .errors import (
    AttributionError,
    ClockAnomalyError,
    EncodingError_,
    EventDriftError,
    EventMissingError,
    FreezeError,
    InvariantError,
    LatticeError,
    PolicyError,
    SessionConflictError,
    StoreCorruptError,
    StoreError,
    StoreIOError,
)
from .events import AtomStatus, FetchResult, SrcState, fetch, register
from .freeze import FTier, FrozenBlock, TierStat, build_frozen_block, frozen_from_plan
from .model import (
    AssemblyPlan,
    BlockKind,
    CompactionEvent,
    DegradationRecord,
    DeltaKind,
    DeltaOp,
    Framing,
    FrozenView,
    Leaf,
    MergeStat,
    Measure,
    Message,
    Payload,
    PayloadBlock,
    Purpose,
    Role,
    SessionStats,
    StoreSnapshot,
    SystemPrompt,
    Topic,
    Turn,
)
from .policy import BoundaryReason, DegradeReason, Policy
from .relevance import ScoredNode, select_relevant, select_subtrees
from .render import (
    MISSING_REPLY_MARKER,
    R_HEADER,
    normalize_prompt,
    render_leaf,
    render_purpose,
    render_skeleton,
    render_subtree,
    render_turn,
)
from .session import Phase, Session, make_system_prompt
from .store import MemoryStore, StorePort, StoreTxn
from .store_sqlite import DDL, SCHEMA_VERSION, SqliteStore, map_error
from .text import (
    body_hash,
    normalize,
    sanitize,
    sha16_text,
    sha256_hex,
    snapshot_id,
    split_tag,
)
from .tokens import (
    CounterMode,
    EstimateCounter,
    TiktokenCounter,
    TokenCounter,
    make_counter,
    tiktoken_available,
)

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION",
    "DDL",
    "map_error",
    "describe_event",
    "tokens_after_boundary",
    "frozen_from_plan",
    "__version__",
    # errors
    "AttributionError",
    "ClockAnomalyError",
    "EncodingError_",
    "EventDriftError",
    "EventMissingError",
    "FreezeError",
    "InvariantError",
    "LatticeError",
    "PolicyError",
    "SessionConflictError",
    "StoreCorruptError",
    "StoreError",
    "StoreIOError",
    # model
    "AssemblyPlan",
    "BlockKind",
    "CompactionEvent",
    "DegradationRecord",
    "DeltaKind",
    "DeltaOp",
    "Framing",
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
    # policy / clock / tokens
    "BoundaryReason",
    "ClockPort",
    "CounterMode",
    "DegradeReason",
    "EstimateCounter",
    "FrozenClock",
    "LogicalClock",
    "Policy",
    "SystemClock",
    "TiktokenCounter",
    "TokenCounter",
    "make_counter",
    "tiktoken_available",
    # text / render
    "MISSING_REPLY_MARKER",
    "R_HEADER",
    "body_hash",
    "body_key",
    "normalize",
    "normalize_prompt",
    "render_leaf",
    "render_purpose",
    "render_skeleton",
    "render_subtree",
    "render_turn",
    "sanitize",
    "sha16_text",
    "sha256_hex",
    "snapshot_id",
    "split_tag",
    # attribution / relevance / freeze
    "FTier",
    "FrozenBlock",
    "NumberAllocator",
    "ScoredNode",
    "TierStat",
    "apply_delta",
    "build_frozen_block",
    "parse_delta",
    "select_relevant",
    "select_subtrees",
    # store
    "MemoryStore",
    "SqliteStore",
    "StorePort",
    "StoreTxn",
    # assembler
    "AssemblyInputs",
    "assemble",
    "assert_prefix_stable",
    "common_prefix_bytes",
    "describe_payload",
    "prefix_violation",
    # compaction / events / degrade / session / arms
    "AtomStatus",
    "CompactionResult",
    "FetchResult",
    "Phase",
    "Session",
    "SrcState",
    "buffer_only_payload",
    "classify",
    "compact",
    "fetch",
    "full_history_payload",
    "last_resort_payload",
    "make_system_prompt",
    "register",
    "ArmProtocol",
    "BaselineArm",
    "LatticeArm",
    "StepResult",
    "assert_arms_aligned",
]
