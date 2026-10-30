"""异常层级。

设计约束（ARCHITECTURE §4.2 / §10）：

* 异常**只在构造期与显式 API 处抛出**。``Session.send()`` / ``Session.observe()``
  内部把 ``Exception`` 全部吞掉（A4 fail-open），但**不吞** ``BaseException``
  —— ``KeyboardInterrupt`` / ``SystemExit`` 必须穿透。
* 继承 ``LatticeError`` 的一切都视为「受控失败」：带中文说明，可直接展示，
  不打印 traceback。
* 每个异常都带 ``where``（文件/行号/字段）信息，延续 ``ctx.py`` 的定位风格。

依赖：仅标准库。本模块不得 import 任何其它内部模块（它是依赖图的叶子）。
"""

from __future__ import annotations


class LatticeError(Exception):
    """受控失败根。带中文说明，直接展示给调用方，不打印 traceback。"""


# ---------------------------------------------------------------------------
# 参数 / 策略
# ---------------------------------------------------------------------------
class PolicyError(LatticeError):
    """策略参数非法。**构造期抛，不吞**（错参数是程序缺陷，A4）。"""


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------
class StoreError(LatticeError):
    """存储层受控失败根。"""


class StoreCorruptError(StoreError):
    """结构 / 不变量损坏（对应 ``ctx.py`` 的 V1–V20）。"""


class SessionConflictError(StoreError):
    """同一 ``session_id`` 被并发驱动（P9 被违反）→ F16。

    **它和 ``StoreCorruptError`` 必须分开**，虽然两者都继承 ``StoreError``：
    同一种原因两个类型、或两种原因一个类型，都会让降级分派误诊。

    * F1（库坏了）说的是"我兜住了，你不用管"——处置是降级 + 继续跑；
    * F16（P9 破了）说的是"**你的进程模型错了，去改它**"——处置是本进程彻底停写。

    合成一个类型的后果是实测过的：``plans`` 的 ``UNIQUE(session_id, generation)``
    冲突被译成 ``StoreCorruptError``，于是运维跑去 ``integrity_check``，而它一定全绿。

    ``op`` 记的是"当时在做什么"（调用点），因为**语义由调用点产生、由类型消费**：
    存储层只有 ``commit_plan`` 会写 ``plans.generation``，所以只有它译出本异常；
    降级层只认类型，不许再读消息文本去猜。
    """

    def __init__(self, message: str, *, op: str | None = None) -> None:
        super().__init__(message)
        self.op = op


class StoreIOError(StoreError):
    """读写失败，含 ENOSPC / EACCES / EROFS。"""

    def __init__(self, message: str, *, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


# ---------------------------------------------------------------------------
# 编码 / 时钟 / 归因
# ---------------------------------------------------------------------------
class EncodingError_(LatticeError):
    """编码异常（对应 ``references/degradation.md`` D7 / D20）。

    尾部下划线是**故意保留的**：ARCHITECTURE §4.2 钉死了这个名字，
    改名会让调用方的 ``except`` 分支静默失效。
    """


class ClockAnomalyError(LatticeError):
    """时钟回退 / 跳变 / 不可用。

    注意：时钟**不参与装配**，所以本异常物理上无法影响 payload 字节（§10.1）。
    """


class AttributionError(LatticeError):
    """调用方给的 T/P/R/E 增量行不合法。"""


# ---------------------------------------------------------------------------
# 事件节点
# ---------------------------------------------------------------------------
class EventDriftError(LatticeError):
    """E 节点内容漂移（``fetch`` 侧：取回内容与登记的指纹不符）。"""


class EventMissingError(LatticeError):
    """E 节点原文缺失（``fetch`` 侧：来源不可达且没有内嵌副本）。"""


# ---------------------------------------------------------------------------
# 内部不变量
# ---------------------------------------------------------------------------
class FreezeError(LatticeError):
    """冻结块构建的内部不变量违反（钉住项超硬上限等）。不应发生。"""


class InvariantError(LatticeError):
    """前缀稳定性等硬断言被破坏。

    仅 ``policy.strict_invariants=True`` 或测试中抛出 —— 生产路径上破坏 I1
    是不可恢复的结构性错误，抛不抛都救不回 ``cached_i``，但测试必须炸。
    """


__all__ = [
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
]
