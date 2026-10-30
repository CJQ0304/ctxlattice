"""Token 计数：精确（tiktoken）/ 估算。

与 MEASUREMENT-SPEC §4 对齐：**精确计数是主表，估算只能作降级且必须标
``mode=estimate``**。这里的取舍是硬的：宁可报「量级判断」，也不许把
``len(text)/4`` 这类经验换算混进主表。

依赖纪律（ARCHITECTURE §3.2 第 4 条）：**本文件是唯一 ``import tiktoken`` 的地方**，
且必须放在 ``try/except ImportError`` 之后 —— 核心包对第三方依赖为零，
``tiktoken`` 只是 ``extras``。

``counter_id`` 为什么是钉死常量而不是运行时读版本号：
它进 ``AssemblyPlan`` 并且**是确定性前提的一部分**（§11.2 —— 计数口径变了，
裁剪点就可能变，字节就可能变）。如果它是 ``tiktoken.__version__``，那么换台机器
升级一次库，历史 payload 的 counter_id 就对不上了，而**没有任何报错**。
真实库版本单独用 ``lib_version`` 暴露，写进报告即可。
"""

from __future__ import annotations

import math
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .errors import LatticeError

if TYPE_CHECKING:
    from .policy import Policy

try:  # pragma: no cover - 取决于环境是否装了 extras
    import tiktoken as _tiktoken
except ImportError:  # pragma: no cover
    _tiktoken = None

# 钉死的 counter_id（写进 AssemblyPlan / 报告 / summary.json）
PINNED_TIKTOKEN_COUNTER_ID = "tiktoken:o200k_base:2024-07"
PINNED_ENCODING = "o200k_base"
DEFAULT_CHARS_PER_TOKEN = 2


class CounterMode(str, Enum):
    EXACT = "exact"          # tiktoken，编码钉死
    ESTIMATE = "estimate"    # 无 tiktoken 时的确定性降级


@runtime_checkable
class TokenCounter(Protocol):
    @property
    def counter_id(self) -> str:
        """例 ``"tiktoken:o200k_base:2024-07"`` / ``"estimate:ceil(chars/2)"``。"""
        ...

    @property
    def mode(self) -> CounterMode:
        ...

    def count(self, text: str) -> int:
        ...

    def encode(self, text: str) -> tuple[int, ...]:
        ...


def tiktoken_available() -> bool:
    return _tiktoken is not None


class TiktokenCounter:
    """精确计数。``import`` 失败则**不可构造**（不是静默退化为估算）。"""

    def __init__(self, encoding: str = PINNED_ENCODING) -> None:
        if _tiktoken is None:
            raise LatticeError(
                "无法构造 TiktokenCounter：当前环境没有安装 tiktoken。"
                "请 `pip install tiktoken`（或 `pip install ctxlattice[bench]`），"
                "或把 policy.counter_prefer 设为 \"estimate\" 显式走估算降级"
                "（此时报告必须标 mode=estimate）")
        self._encoding_name = encoding
        try:
            self._enc = _tiktoken.get_encoding(encoding)
        except Exception as exc:  # noqa: BLE001 - tiktoken 的异常类型不稳定
            raise LatticeError(
                "无法构造 TiktokenCounter：tiktoken 不认识编码 %r（可用值如 o200k_base / "
                "cl100k_base）：%s" % (encoding, exc)) from exc
        self._lib_version = str(getattr(_tiktoken, "__version__", "unknown"))

    @property
    def counter_id(self) -> str:
        # 编码名变了就换 id；否则恒为钉死常量，保证跨环境的 AssemblyPlan 可比。
        if self._encoding_name == PINNED_ENCODING:
            return PINNED_TIKTOKEN_COUNTER_ID
        return "tiktoken:%s:pinned" % self._encoding_name

    @property
    def mode(self) -> CounterMode:
        return CounterMode.EXACT

    @property
    def lib_version(self) -> str:
        """tiktoken 库的真实版本。只进报告，**不进** ``counter_id``。"""
        return self._lib_version

    def count(self, text: str) -> int:
        return len(self._enc.encode_ordinary(text))

    def encode(self, text: str) -> tuple[int, ...]:
        return tuple(self._enc.encode_ordinary(text))


class EstimateCounter:
    """确定性估算：``ceil(len(text) / chars_per_token)``。

    纯标准库、无依赖、跨平台稳定。用 ``len(text)``（字符数）而不是字节数：
    中文 UTF-8 是 3 字节/字，用字节数会让中英混排的估算偏差反向摆动。

    ``encode()`` 抛 ``NotImplementedError`` —— 这是**契约的一部分**：
    估算模式下没有 token id 可比，bench 的 ``cache_prefix_tokens`` 必须改走
    「字节公共前缀 / chars_per_token」并标 ``mode=estimate``。这里给一个假 id
    序列会让缓存命中率看起来精确，实际是编的。
    """

    def __init__(self, chars_per_token: int = DEFAULT_CHARS_PER_TOKEN) -> None:
        if chars_per_token < 1:
            raise LatticeError("EstimateCounter 的 chars_per_token 必须 ≥ 1，实测 %d"
                               % chars_per_token)
        self._cpt = chars_per_token

    @property
    def counter_id(self) -> str:
        return "estimate:ceil(chars/%d)" % self._cpt

    @property
    def mode(self) -> CounterMode:
        return CounterMode.ESTIMATE

    @property
    def chars_per_token(self) -> int:
        return self._cpt

    def count(self, text: str) -> int:
        if not text:
            return 0
        return int(math.ceil(len(text) / float(self._cpt)))

    def encode(self, text: str) -> tuple[int, ...]:
        raise NotImplementedError(
            "EstimateCounter 没有 token id：估算模式无法做逐 id 前缀比对。"
            "请改用「字节公共前缀 / chars_per_token」并在报告标 mode=estimate")


def make_counter(policy: "Policy") -> TokenCounter:
    """按 ``policy`` 选计数器。

    ``counter_prefer="exact"`` 且 tiktoken 可导入 → 精确；
    否则 → 估算，并由**调用方**把 ``mode=estimate`` 写进报告（这里不写，
    因为报告不是本层的职责，而"顺手打一行日志"会变成第二个真相）。
    """
    if policy.counter_prefer == "exact" and _tiktoken is not None:
        return TiktokenCounter(policy.tiktoken_encoding)
    if policy.counter_prefer == "exact":
        # 请求了精确但没有库 → 显式退化（而不是抛）：这是"依赖缺失"，
        # 不是"参数非法"，A4 要求它能继续跑，只要口径标清楚。
        return EstimateCounter()
    return EstimateCounter()


__all__ = [
    "CounterMode",
    "DEFAULT_CHARS_PER_TOKEN",
    "EstimateCounter",
    "PINNED_ENCODING",
    "PINNED_TIKTOKEN_COUNTER_ID",
    "TiktokenCounter",
    "TokenCounter",
    "make_counter",
    "tiktoken_available",
]
