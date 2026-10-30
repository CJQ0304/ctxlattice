"""transcript 读取（TRANSCRIPT-FORMAT）。

刻意做窄：只读「第几轮、谁说了什么」，**不读任何装配指令**。
两臂的差别必须全部由代码产生 —— 剧本里一旦混进预算或期望长度，测出来的就是人的手艺。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..errors import LatticeError
from ..model import Message, Role, Turn


@dataclass(frozen=True, slots=True)
class Transcript:
    name: str
    turns: tuple[Turn, ...]
    meta: Mapping[str, object]
    sha256: str
    locality: str
    attrib: tuple[tuple[int, tuple[str, ...]], ...] = ()

    def attrib_for(self, turn: int) -> tuple[str, ...]:
        for t, lines in self.attrib:
            if t == turn:
                return lines
        return ()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_transcript(jsonl_path: str | Path, meta_path: str | Path | None = None) -> Transcript:
    p = Path(jsonl_path)
    if not p.is_file():
        raise LatticeError("找不到 transcript 文件：%s" % p)
    mp = Path(meta_path) if meta_path else p.with_suffix("").with_suffix(".meta.json")

    rows = []
    with open(p, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LatticeError("%s 第 %d 行不是合法 JSON：%s" % (p.name, lineno, exc))
    if not rows:
        raise LatticeError("%s 是空的 —— transcript 至少要有一轮" % p.name)

    # 按 turn 归组，保持首次出现顺序
    order: list[int] = []
    bucket: dict[int, list[dict]] = {}
    for r in rows:
        idx = int(r["turn"])
        if idx not in bucket:
            bucket[idx] = []
            order.append(idx)
        bucket[idx].append(r)

    if order != list(range(1, len(order) + 1)):
        raise LatticeError(
            "%s 的 turn 必须从 1 起连续，实际为 %s —— 跳号会让「第 N 轮」失去意义"
            % (p.name, order[:12]))

    turns: list[Turn] = []
    for idx in order:
        group = bucket[idx]
        if group[0].get("role") != "user":
            raise LatticeError("%s 第 %d 轮的首条必须是 user" % (p.name, idx))
        user_text = str(group[0]["content"])
        replies = tuple(
            Message(Role(str(g["role"])), str(g["content"])) for g in group[1:]
        )
        pinned = any(bool(g.get("pinned")) for g in group)
        turns.append(Turn(index=idx, user_text=user_text, replies=replies, pinned=pinned))

    meta: Mapping[str, object] = {}
    if mp.is_file():
        with open(mp, "r", encoding="utf-8") as fh:
            meta = json.load(fh)

    attrib: list[tuple[int, tuple[str, ...]]] = []
    ap = p.parent / (p.stem + ".attrib.jsonl")
    if ap.is_file():
        with open(ap, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    attrib.append((int(obj["turn"]), tuple(str(x) for x in obj["lines"])))
                except (json.JSONDecodeError, KeyError) as exc:
                    raise LatticeError("%s 第 %d 行不合法：%s" % (ap.name, lineno, exc))

    return Transcript(
        name=str(meta.get("name") or p.stem),
        turns=tuple(turns),
        meta=meta,
        sha256=_sha256_file(p),
        locality=str(meta.get("locality") or "unknown"),
        attrib=tuple(attrib),
    )
