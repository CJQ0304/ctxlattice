"""事件节点（E）：登记 / 取回 / 三态校验。

E 是**压不动的整块素材**（要逐字改的代码、要逐行核的数据）。库里只存
「摘要 + 指针 + 字节数 + 哈希」，**原文不进库**（"库应当轻，原文应当只有一个出处"）。

三态必须分开看（``references/store-format.md`` 的四种状态）：

| 状态 | 含义 | 影响 |
|---|---|---|
| ``ok`` | 取回内容与登记的字节数、哈希都相符 | 否 |
| ``DRIFT`` | 取回内容与登记不符（来源被改写） | **是** |
| ``MISS`` | 找不到来源（被移走/删除，且没有内嵌副本） | **是** |
| ``STALE-SRC`` | 内容可用，但来源指针已不再指向同一内容 | 否（只提示） |

最后一行是最容易搞错的地方：内嵌副本能让取回成功，但那**不代表指针还指得准**。
「内容校验必须硬、指针状态必须软」，两者不能混成一条 —— 否则「可校验」会退化成
「只要有一份副本就永远绿灯」。

**装配路径永不自动 fetch**（N5 / F14）：F 里的 E 行只放指针行本身，正文不进 payload。
因此「源文件漂移」这一类故障**在物理上进不了装配路径**，只可能在调用方显式取回时出现。
这是有意的故障域收缩 —— 让一整类故障从热路径上消失，而不是在热路径里兜它。

依赖：``store``（端口）/ ``attribution`` / ``model`` / ``text`` / ``errors``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from .errors import (
    AttributionError,
    EventMissingError,
    StoreError,
)
from .model import Leaf, MergeStat, Topic
from .store import write_atom
from .text import (
    check_src,
    check_summary,
    check_tag,
    check_text,
    normalize,
    sanitize,
    sha16_bytes,
)

if TYPE_CHECKING:
    from .store import StorePort


class AtomStatus(str, Enum):
    OK = "ok"
    DRIFT = "drift"
    MISSING = "missing"


class SrcState(str, Enum):
    OK = "ok"
    DRIFT = "stale_src"
    MISSING = "stale_src"


@dataclass(frozen=True, slots=True)
class FetchResult:
    summary: str
    data: bytes | None
    status: AtomStatus
    src_state: SrcState
    desc: str

    @property
    def ok(self) -> bool:
        """内容是否可信。``STALE_SRC`` 不影响它（指针状态软）。"""
        return self.status is AtomStatus.OK


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _base_dir(store: "StorePort") -> str | None:
    """取 ``src`` 相对路径的基准目录。没有就返回 None（不猜 cwd）。"""
    for attr in ("base_dir",):
        value = getattr(store, attr, None)
        if value:
            return os.fspath(value)
    atom = getattr(store, "atom_dir", None)
    if atom:
        return os.path.dirname(os.fspath(atom))
    return None


def resolve_src(store: "StorePort", src: str) -> str | None:
    """把库里的 ``src`` 解析成文件系统路径。解析不了返回 None（不猜）。"""
    if os.path.isabs(src):
        return src
    base = _base_dir(store)
    if base is None:
        return None
    return os.path.join(base, src.replace("/", os.sep))


def relativize(store: "StorePort", path: str) -> str:
    """尽量把绝对路径存成相对库的形态（跨机器可搬）。做不到就存原样。"""
    base = _base_dir(store)
    if not base:
        return path.replace(os.sep, "/")
    try:
        rel = os.path.relpath(path, base)
    except ValueError:  # Windows 跨盘符
        return path.replace(os.sep, "/")
    if rel.startswith(".."):
        return path.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def _read_bytes(path: str) -> bytes | None:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------
def register(
    store: "StorePort",
    *,
    topic: str,
    purpose: str,
    src_path: str,
    summary: str | None,
    tag: str | None,
    embed: bool,
) -> MergeStat:
    """登记一个 E 节点。**src 必须真实存在；指纹现场计算，绝不伪造。**

    为什么「读不到源」必须报错并停下（D17）：E 的全部价值就在于「可校验」。
    为一个读不到的源写一条编出来的 bytes/sha256，等于把校验变成装饰 ——
    而且事后没有任何办法区分「当时就是这样」与「当时就写错了」。
    """
    name_where = "register(topic=%r)" % sanitize(topic, 40)
    check_text(topic, "register 的 topic 名称")
    check_text(purpose, "register 的 purpose 名称")
    check_src(src_path, "register 的 src_path")

    resolved = resolve_src(store, src_path)
    if resolved is None:
        raise EventMissingError(
            "无法解析 src 的基准目录：src=%r 是相对路径，但存储没有提供 base_dir。"
            "请构造存储时给出 base_dir（SqliteStore 默认用库文件所在目录），"
            "或传入绝对路径 —— 猜一个当前工作目录会让同一份库在两台机器上指向不同文件"
            % sanitize(src_path, 120))
    if not os.path.exists(resolved):
        raise EventMissingError(
            "事件节点的源文件不存在：src=%r（解析为 %s）。"
            "按 D17：必须报错并停下，不得为一个读不到的源伪造字节数与哈希"
            % (sanitize(src_path, 120), sanitize(resolved, 200)))
    if os.path.isdir(resolved):
        raise EventMissingError(
            "事件节点的 src 指向目录而不是文件：%s。E 只挂单块素材"
            % sanitize(resolved, 200))

    data = _read_bytes(resolved)
    if data is None:
        raise EventMissingError(
            "事件节点的源文件读不出来（权限或 IO 错误）：%s" % sanitize(resolved, 200))

    text_summary = summary
    if text_summary is None:
        # 摘要缺省用文件名 —— 调用方没给摘要时至少给一个能认出这块素材的名字。
        text_summary = os.path.basename(resolved)
    text_summary = normalize(text_summary)
    try:
        check_summary(text_summary, "register 的 summary")
    except AttributionError as exc:
        raise AttributionError(
            "%s\n（摘要会出现在 F 的 [事件] 行里，必须能被单行渲染）" % exc) from exc
    check_tag(tag, "register 的 tag")

    if embed:
        # 内嵌副本与登记同一次调用里完成：先落副本再写库，
        # 这样"库里有指针但副本没落成"这种半截状态不会出现。
        write_atom(getattr(store, "atom_dir", None), data)

    stored_src = relativize(store, resolved)
    lines = [
        "T %s" % normalize(topic),
        "P %s" % normalize(purpose),
        "E %s | src:%s | bytes:%d | sha256:%s%s"
        % (text_summary, stored_src, len(data), sha16_bytes(data),
           "" if tag is None else " [%s]" % tag),
    ]
    try:
        with store.begin(write=True) as txn:
            stat = txn.apply_delta(lines)
            txn.commit()
    except StoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StoreError("%s：写入事件节点失败：%s" % (name_where, exc)) from exc
    return stat


# ---------------------------------------------------------------------------
# 取回
# ---------------------------------------------------------------------------
def fetch(
    store: "StorePort",
    *,
    topic: str | None,
    purpose: str | None,
    check_only: bool = False,
) -> tuple[FetchResult, ...]:
    """取回 E 节点的原文并校验。**只由调用方显式调用，装配路径不会走到这里。**"""
    snapshot = store.snapshot()
    results: list[FetchResult] = []
    for t, p, leaf in _iter_events(snapshot.topics, topic, purpose):
        results.append(_fetch_one(store, t, p, leaf, check_only=check_only))
    return tuple(results)


def _iter_events(
    topics: Sequence[Topic], topic: str | None, purpose: str | None
):
    want_topic = normalize(topic) if topic is not None else None
    want_purpose = normalize(purpose) if purpose is not None else None
    for t in topics:
        if want_topic is not None and normalize(t.name) != want_topic:
            continue
        for p in t.purposes:
            if want_purpose is not None and normalize(p.name) != want_purpose:
                continue
            for leaf in p.leaves:
                if leaf.kind == "E":
                    yield t, p, leaf


def _fetch_one(
    store: "StorePort", topic: Topic, purpose, leaf: Leaf, *, check_only: bool
) -> FetchResult:
    where = "T%d/P%s/E %s" % (topic.num, purpose.num, sanitize(leaf.text, 40))
    src = leaf.src or ""
    expect_sha = leaf.sha16 or ""
    expect_bytes = leaf.nbytes or 0

    resolved = resolve_src(store, src)
    from_src = _read_bytes(resolved) if resolved is not None else None

    embedded = _read_atom(getattr(store, "atom_dir", None), expect_sha)
    data = from_src if _matches(from_src, expect_sha, expect_bytes) else embedded
    if data is None:
        data = from_src if from_src is not None else embedded

    # —— 指针状态（软）——
    src_state = SrcState.OK if _matches(from_src, expect_sha, expect_bytes) else SrcState.MISSING

    if data is None:
        if resolved is None:
            desc = "%s：src 是相对路径但存储没有 base_dir，无法定位原文" % where
        elif not os.path.exists(resolved):
            desc = "%s：找不到来源 %s（且没有内嵌副本）" % (where, sanitize(src, 120))
        else:
            desc = "%s：来源 %s 读不出来（权限或 IO 错误）" % (where, sanitize(src, 120))
        return FetchResult(summary=leaf.text, data=None,
                           status=AtomStatus.MISSING, src_state=src_state, desc=desc)

    actual_sha = sha16_bytes(data)
    actual_bytes = len(data)
    if actual_sha == expect_sha and actual_bytes == expect_bytes:
        status = AtomStatus.OK
        desc = "%s：内容与登记一致（%d 字节）" % (where, actual_bytes)
        if src_state is not SrcState.OK:
            desc += "；但来源指针已不指向同一内容（STALE-SRC，不影响本次使用）"
    else:
        status = AtomStatus.DRIFT
        desc = (
            "%s：内容漂移 —— 登记 %d 字节/sha256:%s，实际 %d 字节/sha256:%s。"
            "DRIFT 的内容**不得当原文使用**（它已不是你登记的那份）"
            % (where, expect_bytes, expect_sha, actual_bytes, actual_sha))

    if check_only:
        data = None

    return FetchResult(summary=leaf.text, data=data, status=status,
                       src_state=src_state, desc=desc)


def _matches(data: bytes | None, sha: str, nbytes: int) -> bool:
    if data is None:
        return False
    return len(data) == nbytes and sha16_bytes(data) == sha


def _read_atom(atom_dir: str | None, sha16: str) -> bytes | None:
    if not atom_dir or not sha16:
        return None
    path = os.path.join(os.fspath(atom_dir), sha16)
    if not os.path.exists(path):
        return None
    data = _read_bytes(path)
    if data is None:
        return None
    return data if sha16_bytes(data) == sha16 else None


def summarize_results(results: Sequence[FetchResult]) -> str:
    """给调用方的诚实汇总（中文，不会把 DRIFT 说成"取回成功"）。"""
    total = len(results)
    ok = sum(1 for r in results if r.status is AtomStatus.OK)
    drift = sum(1 for r in results if r.status is AtomStatus.DRIFT)
    missing = sum(1 for r in results if r.status is AtomStatus.MISSING)
    stale = sum(1 for r in results if r.src_state is SrcState.DRIFT)
    parts = ["共 %d 个事件节点：ok %d / DRIFT %d / MISS %d" % (total, ok, drift, missing)]
    if stale:
        parts.append("其中 %d 个来源指针已失效（STALE-SRC，仅提示）" % stale)
    if drift or missing:
        parts.append("⚠ DRIFT/MISS 应视为失败")
    return "；".join(parts)


__all__ = [
    "AtomStatus",
    "FetchResult",
    "SrcState",
    "fetch",
    "register",
    "relativize",
    "resolve_src",
    "summarize_results",
]
