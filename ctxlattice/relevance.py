"""确定性相关子树选取（无模型、无 embedding、无浮点）。

约束（§8.1）：同一 ``(query, snapshot, turn_index, policy, last_touch)`` → 同一结果序列。
满足它的手段不是"小心"，而是**把不确定性来源从算法里删掉**：

* 分数全整数，权重是整数常量，不做除法 → 跨平台格式化差异无从渗入排序；
* 排序键只有 ``(-score, path)``，``path`` 是字符串 → 稳定的 tie-break，
  不依赖 ``set`` 迭代序、不依赖 ``dict`` 插入序以外的任何隐式顺序（§8.5）；
* 归一化只做空白折叠 + ASCII 大小写折叠（``text.fold_query``），
  不做 Unicode 归一化 —— 否则同一份存储在 NFC/NFD 环境下会选出不同子树。

分数含义（不是魔法数，是可解释的优先级）：

    score = 8·exact + 4·sub + 3·hit + recency

``exact(8)`` > ``sub(4)`` > ``bigram(3×命中数)`` > ``recency(≤6)`` ——
即「用户本轮明确提到的事物」必须压过「恰好最近动过的东西」。

依赖：``model`` / ``policy`` / ``text``。不 import 存储 / 时钟 / 渲染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .model import Leaf, Purpose, StoreSnapshot, Topic
from .policy import Policy
from .text import check_leaf_kind, fold_query

# 打分权重：整数常量。改它们等于改所有历史边界的选取结果，需评审。
W_EXACT = 8
W_SUB = 4
W_BIGRAM = 3

TOPIC = "topic"
PURPOSE = "purpose"
LEAF = "leaf"


@dataclass(frozen=True, slots=True)
class ScoredNode:
    kind: str            # "topic" | "purpose" | "leaf"
    path: str            # "T3" / "T3/P3.1" / "T3/P3.1/R2"
    score: int           # 全整数，无浮点
    signals: tuple[str, ...]   # 命中了哪些信号（debug / 报告用）


@dataclass(frozen=True, slots=True)
class SubtreeSelection:
    """一个被选中的子树：topic + 若干 purpose + 每个 purpose 选中的叶子。"""

    topic: Topic
    purposes: tuple[Purpose, ...]
    leaves: Mapping[str, tuple[Leaf, ...]]   # purpose.num → 选中的叶子（已按 (-score, 序) 排）
    score: int


# ---------------------------------------------------------------------------
# 特征
# ---------------------------------------------------------------------------
def bigrams(text: str) -> tuple[str, ...]:
    """字符 bigram，去重且保留首次出现顺序（确定性）。"""
    seen: dict[str, None] = {}
    for i in range(len(text) - 1):
        seen.setdefault(text[i:i + 2], None)
    return tuple(seen)


def leaf_path(topic_num: int, purpose_num: str, kind: str, chain_index: int) -> str:
    return "T%d/P%s/%s%d" % (topic_num, purpose_num, kind, chain_index)


def iter_nodes(snapshot: StoreSnapshot) -> Iterable[tuple[str, str, str, object]]:
    """按存储顺序产出 ``(kind, path, own_text, node)``。

    「own_text」是节点**自身**的文本（名称 / 内容），不含子节点 ——
    否则长节点的 bigram 天然更多，选出来的永远是最大那个子树（§8.5 反模式）。
    """
    for topic in snapshot.topics:
        yield TOPIC, topic.path, topic.name, topic
        for purpose in topic.purposes:
            path = "T%d/P%s" % (topic.num, purpose.num)
            yield PURPOSE, path, purpose.name, purpose
            for idx, leaf in enumerate(purpose.leaves):
                kind = check_leaf_kind(leaf.kind)
                yield LEAF, leaf_path(topic.num, purpose.num, kind, idx + 1), leaf.text, (purpose, leaf)


# ---------------------------------------------------------------------------
# 选取
# ---------------------------------------------------------------------------
def select_relevant(
    *,
    query: str,
    snapshot: StoreSnapshot,
    turn_index: int,
    policy: Policy,
    last_touch: Mapping[str, int],
) -> tuple[ScoredNode, ...]:
    """按 §8.2 打分并排序。结果已按 ``(-score, path)`` 排好序。"""
    q = fold_query(query)
    q_bigram_set = set(bigrams(q))
    q_len = len(q)

    out: list[ScoredNode] = []
    for kind, path, own_text, _node in iter_nodes(snapshot):
        n = fold_query(own_text)

        exact = 1 if (len(n) >= 2 and n in q) else 0
        sub = 1 if (q_len >= 2 and q in n) else 0

        node_bigrams = bigrams(n)
        hit = sum(1 for g in node_bigrams if g in q_bigram_set)

        touched = last_touch.get(path)
        if touched is None:
            recency = 0
        else:
            recency = max(0, policy.recency_window - (turn_index - touched))

        score = W_EXACT * exact + W_SUB * sub + W_BIGRAM * hit + recency
        if score < policy.relevance_floor:
            continue

        signals = []
        if exact:
            signals.append("exact")
        if sub:
            signals.append("sub")
        if hit:
            signals.append("bigram:%d" % hit)
        if recency:
            signals.append("recency:%d" % recency)
        out.append(ScoredNode(kind=kind, path=path, score=score, signals=tuple(signals)))

    out.sort(key=lambda n: (-n.score, n.path))
    return tuple(out)


def select_subtrees(
    *,
    nodes: Sequence[ScoredNode],
    snapshot: StoreSnapshot,
    policy: Policy,
) -> tuple[SubtreeSelection, ...]:
    """把散装打分聚成子树（§8.3）。

    散装 R 行脱离父子关系会产生误读（"不要代糖"挂在哪个目的下？），所以
    选取单位是子树：topic → purpose → 叶子，逐层按 ``(-score, 序)`` 收敛。
    """
    # path → score 查表。同名 path 不可能重复（path 含编号；R/E 的段内序由链位置定）
    score_of: dict[str, int] = {}
    for node in nodes:
        prev = score_of.get(node.path)
        if prev is None or node.score > prev:
            score_of[node.path] = node.score

    selections: list[SubtreeSelection] = []
    for topic in snapshot.topics:
        topic_score = score_of.get(topic.path, 0)

        purpose_hits: list[tuple[int, str, Purpose, list[tuple[int, int, Leaf]]]] = []
        for purpose in topic.purposes:
            ppath = "T%d/P%s" % (topic.num, purpose.num)
            leaf_scores: list[tuple[int, int, Leaf]] = []
            for idx, leaf in enumerate(purpose.leaves):
                kind = check_leaf_kind(leaf.kind)
                lpath = leaf_path(topic.num, purpose.num, kind, idx + 1)
                lscore = score_of.get(lpath)
                if lscore is not None and lscore >= policy.relevance_floor:
                    leaf_scores.append((lscore, idx, leaf))
            if not leaf_scores:
                continue
            # purpose.score = max(其下 R 的 score)（§8.3 第 3 步）
            pscore = max(s for s, _i, _l in leaf_scores)
            purpose_hits.append((pscore, ppath, purpose, leaf_scores))

        if not purpose_hits:
            # 只有名称命中（骨架里已经有它），不进 T6
            continue

        topic_score = max([topic_score] + [p[0] for p in purpose_hits])
        purpose_hits.sort(key=lambda item: (-item[0], item[1]))
        purpose_hits = purpose_hits[: policy.max_purposes_per_topic]

        purposes: list[Purpose] = []
        leaves: dict[str, tuple[Leaf, ...]] = {}
        for pscore, _ppath, purpose, leaf_scores in purpose_hits:
            ordered = sorted(leaf_scores, key=lambda item: (-item[0], item[1]))
            leaves[purpose.num] = tuple(leaf for _s, _i, leaf in ordered)
            purposes.append(Purpose(
                num=purpose.num,
                name=purpose.name,
                tag=purpose.tag,
                leaves=leaves[purpose.num],
            ))

        selections.append(SubtreeSelection(
            topic=topic, purposes=tuple(purposes), leaves=leaves, score=topic_score))

    selections.sort(key=lambda s: (-s.score, s.topic.path))
    return tuple(selections[: policy.max_topics_in_f])


__all__ = [
    "LEAF",
    "PURPOSE",
    "TOPIC",
    "ScoredNode",
    "SubtreeSelection",
    "W_BIGRAM",
    "W_EXACT",
    "W_SUB",
    "bigrams",
    "iter_nodes",
    "leaf_path",
    "select_relevant",
    "select_subtrees",
]
