"""SQLite 存储实现 —— **全项目唯一 import ``sqlite3`` 的文件**。

schema 的唯一真相是 ``docs/design/STORE-SCHEMA.md``（11 表 4 视图），本文件的
``DDL`` 逐字照抄它的第二节。这里只解释**为什么这么写**，不重述 DDL。

四条纪律，每一条都对应一个「能跑、能过普通测试、但方案价值归零」的失效模式：

1. **读路径也是事务**（R1）。``store`` / ``topic`` / ``leaf`` 三次 SELECT 若各自
   取快照，并发写会让它们落在不同提交上，拼出「T 是旧的、R 是新的」的混合快照 ——
   渲染字节不可复现，``rev`` 与内容对不上，而结果**看起来完全正常**。
   所以 ``snapshot()`` / ``read_turns()`` 一律 ``BEGIN``（DEFERRED）+ 多 SELECT + ``COMMIT``。
2. **错误分类挂在连接上**（R8 / §6.3）。``SQLITE_READONLY`` 是**粘在连接上**的
   （实测同一连接重试 25 次全灭，``busy_timeout`` 完全无效，因为它根本不是
   ``SQLITE_BUSY``）。所以「只读」不是"重试"，是"这条连接废了"：
   标记 → **下一次新写事务开始前**换连接 → 本次调用如实抛 ``StoreIOError``。
   反过来，把约束违反映射成 ``StoreIOError`` 会让"我们写错了"伪装成"磁盘满了"，
   于是一个真 bug 走 F3 → 会话转只读 → 继续静默服务，永远不浮出水面。
3. **改标记必须 ``COALESCE``**（R2）。``INSERT OR IGNORE`` 会让「同文本 + 新标记」
   退化成 no-op（``[已废]`` 永远打不上，审计链整条断掉）；``SET tag = excluded.tag``
   会把已有的 ``[钉住]`` 清成 NULL（用户明确钉住的内容被老化折叠掉，静默数据丢失）。
   两个方向都不报错。
4. **``commit_plan`` 不碰 ``store``**（I16）。压缩不改键树，因此不得让 ``rev`` 变化。
   ``UPDATE store SET rev = rev + 1`` 是这里最自然、也最错的一行。

PRAGMA 必须在 ``BEGIN`` 之前设置（``foreign_keys`` / ``journal_mode`` 在事务内会被
**静默忽略**）；写事务一律 ``BEGIN IMMEDIATE``（DEFERRED 会「先读后写」，
两进程各持读锁再同时想升级 → ``SQLITE_BUSY`` 且不可重试）。

P9 的边界：本类支持**多实例/多进程并发写同一个库**（P7），但同一个 ``Session``
不可被两个进程驱动 —— 那是会话层属性，数据库拦不住。
"""

from __future__ import annotations

import errno as _errno
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Mapping, Sequence

from .clock import ClockPort, safe_iso
from .errors import (
    InvariantError,
    SessionConflictError,
    StoreCorruptError,
    StoreError,
    StoreIOError,
)
from .model import (
    AssemblyPlan,
    bump_store_reads,
    CompactionEvent,
    DegradationRecord,
    Leaf,
    Measure,
    MergeStat,
    Purpose,
    StoreSnapshot,
    SystemPrompt,
    Topic,
    Turn,
    Message,
    Role,
)
from .text import FMT_SCHEMA, normalize, sha16_text
from .text import body_hash as _body_hash_of
from .text import snapshot_id as _snapshot_id

if TYPE_CHECKING:
    from .attribution import NumberAllocator


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1

#: STORE-SCHEMA §2 的 DDL，逐字照抄（含视图）。改动它 = 改库结构 = 走迁移。
DDL = """
-- ============================================================
-- C1 上下文存储 schema v1
-- ============================================================

-- 1. 库：一个作用域一棵键树。所有内容的「版本」与「指纹」只此一处。
CREATE TABLE IF NOT EXISTS store (
    id          TEXT PRIMARY KEY,
    scope       TEXT,
    top_text    TEXT,
    fmt_version TEXT    NOT NULL DEFAULT '1',
    -- rev = 「键树内容真正改过几次」，是 body_hash 的函数（P4）。
    -- I16：压缩 / 边界 / 写 plans / 写 event 都**不改 rev**；commit_plan 里禁止 `rev = rev + 1`。
    rev         INTEGER NOT NULL DEFAULT 0 CHECK (rev >= 0),
    body_hash   TEXT    NOT NULL
                CHECK (length(body_hash) = 64 AND body_hash NOT GLOB '*[^0-9a-f]*'),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (scope IS NULL OR (
              length(scope) <= 64
          AND substr(scope, 1, 4) IN ('user', 'proj', 'task')
          AND (length(scope) = 4 OR substr(scope, 5, 1) IN (':', '/')))),
    CHECK (top_text IS NULL OR (
              length(top_text) BETWEEN 1 AND 500
          AND instr(top_text, '[') = 0 AND instr(top_text, ']') = 0))
);

-- 2. 编号分配器：**只增不减**。编号复用的唯一防线。
CREATE TABLE IF NOT EXISTS num_alloc (
    store_id   TEXT    NOT NULL REFERENCES store(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('T', 'P')),
    parent_num INTEGER NOT NULL DEFAULT 0 CHECK (parent_num >= 0),  -- P 的父事物号；T 恒 0
    last_num   INTEGER NOT NULL DEFAULT 0 CHECK (last_num >= 0),  -- 0 = 尚未分配
    PRIMARY KEY (store_id, kind, parent_num)
) WITHOUT ROWID;

-- 3. T 事物
CREATE TABLE IF NOT EXISTS topic (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   TEXT    NOT NULL REFERENCES store(id) ON DELETE CASCADE,
    num        INTEGER NOT NULL CHECK (num >= 1),
    name       TEXT    NOT NULL,
    name_norm  TEXT    NOT NULL,
    tag        TEXT,
    state      TEXT    NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retired')),
    created_at TEXT    NOT NULL,
    UNIQUE (store_id, num),
    CHECK (length(name) BETWEEN 1 AND 200),
    CHECK (instr(name, '[') = 0 AND instr(name, ']') = 0),   -- V12
    CHECK (instr(name, char(9)) = 0),                        -- V11（制表符，最常见污染）
    CHECK (length(trim(name_norm)) >= 1),
    CHECK (tag IS NULL OR (length(tag) BETWEEN 1 AND 12
           AND instr(tag, '[') = 0 AND instr(tag, ']') = 0
           AND instr(tag, char(9)) = 0))
);
CREATE INDEX IF NOT EXISTS ix_topic_name ON topic(store_id, name_norm);

-- 4. P 目的（编号 P<事物号>.<序号>，序号按父事物各自递增）
CREATE TABLE IF NOT EXISTS purpose (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id   INTEGER NOT NULL REFERENCES topic(id) ON DELETE CASCADE,
    num        INTEGER NOT NULL CHECK (num >= 1),
    name       TEXT    NOT NULL,
    name_norm  TEXT    NOT NULL,
    tag        TEXT,
    state      TEXT    NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retired')),
    created_at TEXT    NOT NULL,
    UNIQUE (topic_id, num),
    CHECK (length(name) BETWEEN 1 AND 200),
    CHECK (instr(name, '[') = 0 AND instr(name, ']') = 0),
    CHECK (instr(name, char(9)) = 0),
    CHECK (length(trim(name_norm)) >= 1),
    CHECK (tag IS NULL OR (length(tag) BETWEEN 1 AND 12
           AND instr(tag, '[') = 0 AND instr(tag, ']') = 0
           AND instr(tag, char(9)) = 0))
);
CREATE INDEX IF NOT EXISTS ix_purpose_name ON purpose(topic_id, name_norm);

-- 5. 叶子：R 与 E 同表。二者是同一有序兄弟链上的两种节点，
--    分表就得靠 union + 共享序号重排，是确定性渲染的直接破坏源。
CREATE TABLE IF NOT EXISTS leaf (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose_id INTEGER NOT NULL REFERENCES purpose(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('R', 'E')),   -- 白名单，未知种类无法落库
    text       TEXT    NOT NULL,
    text_norm  TEXT    NOT NULL,
    tag        TEXT,
    src        TEXT,
    nbytes     INTEGER,
    sha16      TEXT,
    state      TEXT    NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retired')),
    created_at TEXT    NOT NULL,
    CHECK (length(text) BETWEEN 1 AND 200),                   -- V10 / V18
    CHECK (instr(text, '[') = 0 AND instr(text, ']') = 0),    -- V12
    CHECK (kind = 'R' OR instr(text, '|') = 0),               -- V18：摘要禁半角竖线
    CHECK ((kind = 'E') = (src IS NOT NULL
                           AND nbytes IS NOT NULL
                           AND sha16 IS NOT NULL)),           -- E 四段必须齐全，R 必须全空
    CHECK (src   IS NULL OR length(src) BETWEEN 1 AND 400),
    CHECK (nbytes IS NULL OR nbytes >= 0),
    CHECK (sha16 IS NULL OR (length(sha16) = 16 AND sha16 NOT GLOB '*[^0-9a-f]*')),
    CHECK (tag   IS NULL OR (length(tag) BETWEEN 1 AND 12
           AND instr(tag, '[') = 0 AND instr(tag, ']') = 0
           AND instr(tag, char(9)) = 0))
);

-- 幂等归并的唯一真相：同 purpose 下「同归一化文本的 R」只能有一条活行。
CREATE UNIQUE INDEX IF NOT EXISTS ux_leaf_r
    ON leaf(purpose_id, text_norm) WHERE kind = 'R' AND state = 'live';
-- E 的身份是 (摘要, src)：换源文件是新增，不是改内容。
CREATE UNIQUE INDEX IF NOT EXISTS ux_leaf_e
    ON leaf(purpose_id, text_norm, src) WHERE kind = 'E' AND state = 'live';
-- 渲染取活叶子的顺序索引：索引本身即输出顺序，免排序。
CREATE INDEX IF NOT EXISTS ix_leaf_purpose ON leaf(purpose_id, id);
-- 老化只筛「有标记的活叶子」，partial 让索引体积 ∝ 有标记数而非全部叶子。
CREATE INDEX IF NOT EXISTS ix_leaf_lifecycle
    ON leaf(tag) WHERE state = 'live' AND tag IS NOT NULL;

-- 6. 会话 = bench 的一臂一次跑。arm 与计费参数**只放这里**，见第四节 R 组说明。
CREATE TABLE IF NOT EXISTS session (
    id              TEXT PRIMARY KEY,
    store_id        TEXT    NOT NULL REFERENCES store(id) ON DELETE CASCADE,
    arm             TEXT    NOT NULL CHECK (arm IN ('A', 'B')),
    transcript_hash TEXT    NOT NULL CHECK (length(transcript_hash) = 64),
    tokenizer       TEXT    NOT NULL,                       -- 如 'o200k_base@<ver>'，钉死可复算
    price_p         REAL    NOT NULL DEFAULT 1.0 CHECK (price_p > 0),
    cache_delta     REAL    NOT NULL DEFAULT 0.1
                    CHECK (cache_delta > 0 AND cache_delta <= 1),
    mode            TEXT    NOT NULL DEFAULT 'offline'
                    CHECK (mode IN ('offline', 'live', 'estimate')),
    turn_seq        INTEGER NOT NULL DEFAULT 0 CHECK (turn_seq >= 0),  -- 轮次号分配器
    config_json     TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
-- bench 配对校验：同一 transcript 下必须恰好有 A、B 两臂；聚合也按 arm 分组。
CREATE INDEX IF NOT EXISTS ix_session_arm ON session(arm, transcript_hash);

-- 7. 原始轮次：装配「最近 K 轮原文」的唯一来源。
--    一轮 = 1 条 user（part 0）+ 其后若干条 assistant/tool（part 1..n），对齐 ARCHITECTURE 的 Turn。
CREATE TABLE IF NOT EXISTS turn (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    turn_no      INTEGER NOT NULL CHECK (turn_no >= 1),
    part         INTEGER NOT NULL DEFAULT 0 CHECK (part >= 0),  -- 同轮内第几条，0 起
    role         TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content      TEXT    NOT NULL,
    content_hash TEXT    NOT NULL CHECK (length(content_hash) = 64),
    token_count  INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    pinned       INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    created_at   TEXT    NOT NULL,
    UNIQUE (session_id, turn_no, part),
    -- 一轮恰好一条 user（part 0），其余全是 assistant/tool —— 结构上钉死 Turn 的形状
    CHECK (part > 0 OR role = 'user'),
    CHECK (part = 0 OR role IN ('assistant', 'tool'))
);

-- 8. 装配计划（= 压缩边界 + 冻结块）。按 generation 追加，**永不覆盖**（P8）。表名用复数 `plans`，与架构稿一致。
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    schema        INTEGER NOT NULL DEFAULT 1 CHECK (schema >= 1),
    generation    INTEGER NOT NULL CHECK (generation >= 1),   -- 每次压缩 +1
    epoch_turn    INTEGER NOT NULL CHECK (epoch_turn >= 1),   -- 本次 F 冻结所在的轮次（= 边界 turn_no）
    r_first_turn  INTEGER NOT NULL CHECK (r_first_turn >= 1), -- R 段起始轮（含）
    frozen_text   TEXT    NOT NULL CHECK (length(frozen_text) >= 1),
    frozen_sha16  TEXT    NOT NULL
                  CHECK (length(frozen_sha16) = 16 AND frozen_sha16 NOT GLOB '*[^0-9a-f]*'),
    frozen_tokens INTEGER NOT NULL CHECK (frozen_tokens >= 0),
    tokens_before INTEGER NOT NULL CHECK (tokens_before >= 0), -- 边界前 payload token（测量用）
    tokens_after  INTEGER NOT NULL CHECK (tokens_after  >= 0), -- 边界后 payload token（测量用）
    system_sha16  TEXT    NOT NULL CHECK (length(system_sha16) = 16),
    -- 派生量：sha256("ctxsnap1" \x1f schema \x1f system_sha16 \x1f frozen_sha16)[:16]（见 4.4）。
    -- 只覆盖 S+F 的字节来源；不含键树 / rev / rowid / 时间戳 / generation。**纯审计字段，不作闸门**。
    snapshot_id   TEXT    NOT NULL CHECK (length(snapshot_id) = 16),
    -- 审计字段：写本行时的 store.rev 快照。**只写不判**（4.4 禁令 1）。
    store_rev     INTEGER NOT NULL CHECK (store_rev >= 0),
    counter_id    TEXT    NOT NULL,
    policy_digest TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (session_id, generation),
    CHECK (r_first_turn <= epoch_turn + 1)     -- R 段不能从边界之后起步
);
-- 冻结块按内容寻址：同哈希只存一份，跨会话可复用。
CREATE INDEX IF NOT EXISTS ix_plans_frozen ON plans(frozen_sha16);

-- 9. system 提示（两臂必须逐字节相同，故留存正文以便校验 system_sha16）
CREATE TABLE IF NOT EXISTS system_prompt (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    sha16      TEXT    NOT NULL
               CHECK (length(sha16) = 16 AND sha16 NOT GLOB '*[^0-9a-f]*'),
    text       TEXT    NOT NULL CHECK (length(text) >= 1),
    source     TEXT    NOT NULL,     -- "builtin:prompts/system.md" | "caller"
    created_at TEXT    NOT NULL,
    UNIQUE (session_id, sha16)
);

-- 10. 事件流水（压缩事件 + 降级记录）。只追加，报告与 SessionStats 的事实来源。
CREATE TABLE IF NOT EXISTS event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL CHECK (kind IN ('compaction', 'degradation')),
    at_turn       INTEGER NOT NULL CHECK (at_turn >= 1),
    reason        TEXT    NOT NULL CHECK (length(reason) BETWEEN 1 AND 64),
    -- compaction 专属
    generation    INTEGER CHECK (generation IS NULL OR generation >= 1),
    frozen_sha16  TEXT    CHECK (frozen_sha16 IS NULL
                                 OR (length(frozen_sha16) = 16
                                     AND frozen_sha16 NOT GLOB '*[^0-9a-f]*')),
    frozen_tokens INTEGER CHECK (frozen_tokens IS NULL OR frozen_tokens >= 0),
    included_json TEXT,              -- tier → 纳入条数（T1..T7）
    omitted_json  TEXT,              -- tier → 因预算丢弃条数（诚实记账）
    aborted       INTEGER NOT NULL DEFAULT 0 CHECK (aborted IN (0, 1)),
    abort_reason  TEXT,
    -- degradation 专属
    at_tick       INTEGER CHECK (at_tick IS NULL OR at_tick >= 0),  -- 逻辑时钟，非墙钟
    detail        TEXT    CHECK (detail IS NULL OR length(detail) <= 200),
    snapshot_id   TEXT,
    fallback      TEXT    CHECK (fallback IS NULL
                                 OR fallback IN ('full_history', 'buffer_only', 'last_resort')),
    created_at    TEXT    NOT NULL,
    -- 两类事件各自的必填组，缺一即拒（同 leaf 的 kind 判据）
    CHECK (kind = 'degradation'
           OR (generation IS NOT NULL AND frozen_sha16 IS NOT NULL
               AND frozen_tokens IS NOT NULL AND included_json IS NOT NULL
               AND omitted_json IS NOT NULL)),
    CHECK (kind = 'compaction'
           OR (at_tick IS NOT NULL AND detail IS NOT NULL AND fallback IS NOT NULL)),
    CHECK (aborted = 0 OR abort_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_event_session ON event(session_id, at_turn);

-- 11. 测点
CREATE TABLE IF NOT EXISTS measure (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    turn_no       INTEGER NOT NULL CHECK (turn_no >= 1),
    attempt       INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    input_tokens  INTEGER NOT NULL CHECK (input_tokens >= 0),   -- 实际发出的 gross input
    cached_tokens INTEGER NOT NULL DEFAULT 0
                  CHECK (cached_tokens >= 0 AND cached_tokens <= input_tokens),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    payload_hash  TEXT,        -- 整包 sha256：证明两臂差异只在历史段
    system_hash   TEXT,        -- system 首段 sha256：证明「逐字节相同」
    latency_ms    INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    ok            INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0, 1)),
    created_at    TEXT    NOT NULL,
    UNIQUE (session_id, turn_no, attempt)   -- 重试不虚增样本
);

-- ============================================================
-- 视图
-- ============================================================

-- 逐轮计费口径（MEASUREMENT-SPEC 第 3 节的公式，落在库里而非报告脚本里）
CREATE VIEW IF NOT EXISTS v_measure AS
SELECT m.id,
       m.session_id,
       s.arm,
       s.transcript_hash,
       s.mode,
       s.price_p,
       s.cache_delta,
       m.turn_no,
       m.attempt,
       m.input_tokens                              AS gross_input,
       m.cached_tokens                             AS cached,
       m.input_tokens - m.cached_tokens            AS billable,
       (m.input_tokens - m.cached_tokens) * s.price_p
         + m.cached_tokens * s.price_p * s.cache_delta AS cost_eq,
       m.output_tokens,
       m.payload_hash,
       m.system_hash,
       m.latency_ms,
       m.ok,
       m.created_at,
       EXISTS (SELECT 1 FROM plans p
                WHERE p.session_id = m.session_id
                  AND p.epoch_turn = m.turn_no)   AS is_boundary
FROM measure m
JOIN session s ON s.id = m.session_id;

-- 每轮的窗口身份：raw（未压缩原文）/ frozen（已进冻结块）。
-- 边界一律取**最大 generation 的那条 plan**，不能用 MAX(epoch_turn)——
-- 重压时 generation 递增但 epoch_turn 不一定单调（见 R7）。
CREATE VIEW IF NOT EXISTS v_turn_boundary AS
SELECT t.session_id,
       t.turn_no,
       MAX(t.part) + 1 AS parts,
       SUM(t.token_count) AS tokens,
       (SELECT p.epoch_turn FROM plans p
         WHERE p.session_id = t.session_id
         ORDER BY p.generation DESC LIMIT 1) AS boundary_turn,
       CASE WHEN t.turn_no <= COALESCE(
                 (SELECT p.epoch_turn FROM plans p
                   WHERE p.session_id = t.session_id
                   ORDER BY p.generation DESC LIMIT 1), 0)
            THEN 'frozen' ELSE 'raw' END AS window_state
FROM turn t
GROUP BY t.session_id, t.turn_no;

-- 会话级统计（对应 ARCHITECTURE 的 SessionStats，全部现算，不落冗余列）
CREATE VIEW IF NOT EXISTS v_session_stats AS
SELECT s.id AS session_id,
       (SELECT COUNT(DISTINCT t.turn_no) FROM turn t WHERE t.session_id = s.id)      AS turns,
       (SELECT COUNT(*) FROM event e WHERE e.session_id = s.id
          AND e.kind = 'compaction')                                                 AS boundaries,
       (SELECT COUNT(*) FROM event e WHERE e.session_id = s.id
          AND e.kind = 'degradation')                                                AS degradations,
       (SELECT COUNT(*) FROM plans p WHERE p.session_id = s.id)                       AS plan_generations,
       (SELECT COALESCE(MAX(m.input_tokens), 0) FROM measure m
         WHERE m.session_id = s.id)                                                  AS peak_payload_tokens,
       (SELECT COALESCE(m.input_tokens, 0) FROM measure m
         WHERE m.session_id = s.id ORDER BY m.turn_no DESC, m.attempt DESC LIMIT 1)   AS last_payload_tokens
FROM session s;

-- 头部计数（V13 / V17 的库内对照）
CREATE VIEW IF NOT EXISTS v_key_counts AS
SELECT t.store_id,
       COUNT(*)                       AS n_topic,
       SUM(CASE WHEN t.state = 'live' THEN 1 ELSE 0 END) AS n_topic_live
FROM topic t
GROUP BY t.store_id;
"""


# ---------------------------------------------------------------------------
# SQLite 错误码（用错误码优先，消息文本只做兜底：文本会随版本/本地化变化）
# ---------------------------------------------------------------------------
_SQLITE_READONLY = 8
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_SQLITE_IOERR = 10
_SQLITE_CORRUPT = 11
_SQLITE_FULL = 13
_SQLITE_CANTOPEN = 14
_SQLITE_NOTADB = 26

#: 同步盘/网络盘：``-shm`` 依赖 mmap 与文件锁语义，放这里会把偶发只读变成常态，
#: 且是「单机跑得好好的、换台机器就丢数据」的形态。启动时显式告警。
_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud",
                 "sharepoint", "\\\\", "//")


def _err_code(exc: BaseException) -> int | None:
    """SQLite 扩展错误码的**基础码**（``8 | n<<8`` 的低 8 位）。"""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        return None
    return int(code) & 0xFF


def _err_text(exc: BaseException) -> str:
    return str(exc).lower()


def _is_readonly(exc: BaseException) -> bool:
    return _err_code(exc) == _SQLITE_READONLY or "readonly" in _err_text(exc)


def _is_busy(exc: BaseException) -> bool:
    code = _err_code(exc)
    if code is not None:
        return code in (_SQLITE_BUSY, _SQLITE_LOCKED)
    text = _err_text(exc)
    return "locked" in text or "busy" in text


def _is_full(exc: BaseException) -> bool:
    return _err_code(exc) == _SQLITE_FULL or "disk is full" in _err_text(exc)


def _is_corrupt(exc: BaseException) -> bool:
    code = _err_code(exc)
    if code is not None:
        return code in (_SQLITE_CORRUPT, _SQLITE_NOTADB)
    text = _err_text(exc)
    return ("malformed" in text or "not a database" in text
            or "no such table" in text or "no such column" in text)


def _is_io_error(exc: BaseException) -> bool:
    code = _err_code(exc)
    if code is not None:
        return code in (_SQLITE_IOERR, _SQLITE_CANTOPEN)
    text = _err_text(exc)
    return "disk i/o" in text or "unable to open" in text


def map_error(exc: BaseException, *, op: str) -> StoreError:
    """把 SQLite 异常映射成本 SDK 的受控异常（**错误分类的唯一出口**）。

    映射表照 STORE-SCHEMA §6.3。最关键的一行是 `IntegrityError → StoreCorruptError`：
    若把约束违反映射成 ``StoreIOError``，它走 F3（转只读、继续静默服务），
    于是「写入逻辑有个真 bug」被伪装成「磁盘暂时满了」，payload 照常产出，
    永远不浮出水面。而 ``StoreCorruptError`` 走 F1：跳过存储、内存缓冲装配、
    断言失败、测试红、问题当场暴露。

    一句话：``StoreIOError`` 是"环境坏了，等等会好"；约束违反是"我们写错了，
    等多久都不会好"。
    """
    if isinstance(exc, StoreError):
        return exc
    where = "（%s）" % op
    if isinstance(exc, sqlite3.IntegrityError):
        return StoreCorruptError(
            "写入违反库约束%s：%s。这是代码 bug 或坏数据，不是 IO 故障 —— "
            "重试一万次也不会好，必须当场暴露" % (where, exc))
    if isinstance(exc, sqlite3.OperationalError):
        if _is_readonly(exc):
            return StoreIOError(
                "连接已被标记为只读%s：%s。SQLITE_READONLY 粘在连接上，"
                "重试无效（实测同一连接 25/25 全灭），需要换一条连接" % (where, exc),
                errno=_errno.EACCES)
        if _is_full(exc):
            return StoreIOError(
                "磁盘已满%s：%s。按 F3 转只读模式，本轮 payload 不受影响"
                % (where, exc), errno=_errno.ENOSPC)
        if _is_corrupt(exc):
            return StoreCorruptError("库文件损坏%s：%s" % (where, exc))
        if _is_busy(exc):
            return StoreIOError(
                "写锁等待超时%s：%s。busy_timeout 内 SQLite 已自行等待过，"
                "仍失败说明有长事务持锁" % (where, exc), errno=_errno.EBUSY)
        if _is_io_error(exc):
            return StoreIOError("库文件 I/O 失败%s：%s" % (where, exc),
                                errno=getattr(exc, "errno", None))
    if isinstance(exc, sqlite3.DatabaseError):
        return StoreError("SQLite 失败%s：%s" % (where, exc))
    return StoreError("存储层未分类失败%s：%s: %s"
                      % (where, type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# SqliteStore
# ---------------------------------------------------------------------------
class SqliteStore:
    """SQLite 实现。**单实例不保证线程安全**；跨实例/跨进程并发写见 P7。

    ``session_id`` / ``arm`` / ``transcript_hash`` 是 ``session`` 表的必填列
    （``turn`` / ``plans`` / ``event`` 都靠外键挂在它下面）。核心 SDK 只用得到
    「一个会话」，bench 则需要显式给出真实的 ``transcript_hash`` 与 ``arm``：

    * ``transcript_hash`` 缺省时按 ``sha256("ctxlattice:session:" + session_id)``
      生成 —— 它是**占位口径**，只保证 NOT NULL + 64 位十六进制。
      bench 必须显式传入，否则 STORE-SCHEMA §10.1 的第 ⑤ 条（两臂配对校验）
      会读到假哈希而永远通过。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        store_id: str = "default",
        session_id: str | None = None,
        scope: str | None = None,
        top_text: str | None = None,
        arm: str = "B",
        transcript_hash: str | None = None,
        tokenizer: str = "o200k_base",
        price_p: float = 1.0,
        cache_delta: float = 0.1,
        mode: str = "offline",
        atom_dir: str | os.PathLike[str] | None = None,
        base_dir: str | os.PathLike[str] | None = None,
        clock: ClockPort | None = None,
        busy_timeout_ms: int = 5000,
        connect_attempts: int = 4,
    ) -> None:
        self.path = os.fspath(path)
        self.store_id = store_id
        self.session_id = session_id or "s-" + store_id
        self.scope = scope
        self.top_text = top_text
        self.arm = arm
        self.transcript_hash = transcript_hash or hashlib.sha256(
            ("ctxlattice:session:" + self.session_id).encode("utf-8")).hexdigest()
        self.tokenizer = tokenizer
        self.price_p = float(price_p)
        self.cache_delta = float(cache_delta)
        self.mode = mode
        self.atom_dir = (None if atom_dir is None
                         else os.fspath(atom_dir))
        if self.atom_dir is None:
            self.atom_dir = self.path + ".atoms"
        self.base_dir = None if base_dir is None else os.fspath(base_dir)
        self._clock = clock
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._connect_attempts = max(1, int(connect_attempts))
        self._conn: sqlite3.Connection | None = None
        #: 连接级健康状态：``readonly`` 粘在连接上，所以状态挂在**连接**上，
        #: 不是挂在语句或事务上（STORE-SCHEMA §6.2）。
        self._conn_dead = False
        self._opened = False
        self.recycle_count = 0        # 自愈次数（>0 正常，说明防御生效）

    # -- 生命周期 ---------------------------------------------------------
    def open(self) -> None:
        if self._opened:
            return
        _warn_if_sync_folder(self.path)
        self._conn = self._connect_with_probe()
        self._migrate(self._conn)
        self._ensure_rows(self._conn)
        self._opened = True

    def close(self) -> None:
        self._discard_conn()
        self._opened = False

    def __enter__(self) -> "SqliteStore":
        self.open()
        return self

    def __exit__(self, et, ev, tb) -> bool:
        self.close()
        return False

    # -- 读 ---------------------------------------------------------------
    def snapshot(self) -> StoreSnapshot:
        """一致只读视图（P1）。**多 SELECT 必须在同一个读事务里**（R1）。"""
        bump_store_reads()
        conn = self._ensure_conn()
        self._exec(conn, "BEGIN", op="snapshot/begin")
        try:
            store_row = self._one(conn, "SELECT * FROM store WHERE id = ?",
                                  (self.store_id,), op="snapshot/store")
            if store_row is None:
                raise StoreCorruptError(
                    "库里没有 store_id=%r 这一行：库文件不是本 SDK 建的，"
                    "或者被外部删过" % self.store_id)
            topics = self._load_topics(conn)
            plan = self._latest_plan(conn)
            system = self._latest_system(conn)
            sess = self._one(conn, "SELECT turn_seq FROM session WHERE id = ?",
                             (self.session_id,), op="snapshot/session")
            agg = self._one(
                conn,
                "SELECT MIN(turn_no) AS lo, MAX(turn_no) AS hi, "
                "COUNT(DISTINCT turn_no) AS n FROM turn WHERE session_id = ?",
                (self.session_id,), op="snapshot/turns")
            event_count = self._scalar(
                conn, "SELECT COUNT(*) FROM event WHERE session_id = ?",
                (self.session_id,), op="snapshot/events")
        finally:
            self._exec(conn, "COMMIT", op="snapshot/commit")

        lo = int(agg["lo"]) if agg and agg["lo"] is not None else 0
        hi = int(agg["hi"]) if agg and agg["hi"] is not None else 0
        n = int(agg["n"]) if agg and agg["n"] is not None else 0
        # P3 的第二处常开断言：MIN==1 且 MAX==COUNT(DISTINCT) 即「1..N 无洞」。
        if hi and (lo != 1 or hi != n):
            raise StoreCorruptError(
                "轮次不连续：最小轮次 %d（应为 1），最大轮次 %d，去重计数 %d。"
                "整库聚合断言失败说明轮次被外部删改过" % (lo, hi, n))

        if plan is not None and sha16_text(plan.frozen_text) != plan.frozen_sha16:
            raise StoreCorruptError(
                "第 %d 代计划落库的冻结块被改坏：frozen_sha16=%s，正文实测 %s。"
                "只校验最大 generation 那一行（旧行的 snapshot_id 与当前不一致是"
                "设计意图，不是损坏）"
                % (plan.generation, plan.frozen_sha16, sha16_text(plan.frozen_text)))

        if sess is None:
            raise StoreCorruptError(
                "库里没有 session_id=%r 这一行：turn/plans/event 都挂在它下面，"
                "缺了它说明库被外部改过" % self.session_id)

        return StoreSnapshot(
            snapshot_id=_snapshot_id(
                schema=(plan.schema if plan is not None else FMT_SCHEMA),
                system_sha16=(plan.system_sha16 if plan is not None
                              else (system.sha16 if system is not None else "")),
                frozen_sha16=(plan.frozen_sha16 if plan is not None else ""),
            ),
            rev=int(store_row["rev"]),
            topics=topics,
            turn_lo=lo,
            turn_hi=hi,
            plan=plan,
            system=system,
            event_count=int(event_count),
            top_text=store_row["top_text"],
            scope=store_row["scope"],
        )

    def read_turns_with_hashes(
        self, lo: int, hi: int
    ) -> tuple[tuple[Turn, ...], Mapping[tuple[int, int], str]]:
        """★ 一次读事务同时交出**正文与指纹**（P10 的唯一安全入口）。

        为什么必须是"一个方法、一个事务"：分开两次调用时，两次之间可能插进一次
        外部提交，于是正文来自快照 A、指纹来自快照 B —— 两边都是"正确读到的数据"，
        产出却是"正文与指纹错版"。那个结论既可能是真 P9，也可能是纯粹的读时序，
        **在证据上无法区分**。而 F16 的动作不可逆（本进程永久停写），误判一次
        进程就废了，且表现成"压缩再也没发生过"，与"话题稳定所以不需要压缩"
        一模一样（STORE-SCHEMA §10.3 第 8 条 / ARCHITECTURE §10.6）。

        ``read_turns`` 是这个组合入口的投影（一行委托）：**端口上只留一条读路径**，
        跨快照错版在结构上不可能发生，而不是靠调用方自觉。端口上也**没有**单独的
        "只取指纹"成员 —— 它能被安全使用的前提恰好是"取指纹的同时也读正文"，
        那就不该是一个独立入口。
        """
        if lo < 1:
            raise StoreError(
                "read_turns_with_hashes 的 lo 必须 ≥ 1（轮次号 1 起），实测 %d" % lo)
        if hi < lo:
            return (), {}
        bump_store_reads()
        conn = self._ensure_conn()
        self._exec(conn, "BEGIN", op="read_turns_with_hashes/begin")
        try:
            rows = self._all(
                conn,
                "SELECT turn_no, part, role, content, pinned, content_hash FROM turn "
                "WHERE session_id = ? AND turn_no BETWEEN ? AND ? "
                "ORDER BY turn_no, part",
                (self.session_id, lo, hi), op="read_turns_with_hashes")
        finally:
            self._exec(conn, "COMMIT", op="read_turns_with_hashes/commit")

        grouped: list[list] = []
        last_no = None
        for row in rows:
            if row["turn_no"] != last_no:
                grouped.append([])
                last_no = row["turn_no"]
            grouped[-1].append(row)

        turns: list[Turn] = []
        for parts in grouped:
            head = parts[0]
            if head["part"] != 0:
                raise StoreCorruptError(
                    "第 %d 轮缺少 part=0（user 行）：一轮必须恰好由一条 user 起头"
                    % head["turn_no"])
            replies = tuple(
                Message(role=Role(r["role"]), text=r["content"])
                for r in parts[1:]
            )
            turns.append(Turn(
                index=int(head["turn_no"]),
                user_text=head["content"],
                replies=replies,
                pinned=any(bool(r["pinned"]) for r in parts),
            ))

        expected = hi - lo + 1
        if len(turns) != expected:
            raise StoreCorruptError(
                "read_turns 区间 %d..%d 应有 %d 轮，实测 %d 轮：轮次有洞。"
                "R 段依赖轮次连续，有洞即不可装配" % (lo, hi, expected, len(turns)))
        if [t.index for t in turns] != list(range(lo, hi + 1)):
            raise StoreCorruptError(
                "read_turns 区间 %d..%d 的轮次号不是逐值出现：实测 %s"
                % (lo, hi, [t.index for t in turns]))

        hashes = {
            (int(r["turn_no"]), int(r["part"])): str(r["content_hash"])
            for r in rows
        }
        return tuple(turns), hashes

    def read_turns(self, lo: int, hi: int) -> tuple[Turn, ...]:
        """闭区间取轮次，**有洞即 ``StoreCorruptError``**（P3 的主力断言）。

        上层是 fail-open 的：``StoreCorruptError`` → F1 → ``buffer_only`` payload。
        所以这里不必"温柔处理"，直接抛。实现委托给
        ``read_turns_with_hashes``，免得同一段读逻辑有两个快照口径。
        """
        return self.read_turns_with_hashes(lo, hi)[0]

    # 端口上**没有** ``read_turn_hashes``（团队裁定）：它的调用者是 0，而它的
    # docstring 只能靠一句「不要单独用」来定义 —— 一个必须靠禁令才能安全使用的
    # 端口成员，本身就是一片误用面。指纹只从 ``read_turns_with_hashes`` 出来，
    # 与正文同快照（P10）。

    # -- 写 ---------------------------------------------------------------
    def begin(self, *, write: bool = False) -> "_SqliteTxn":
        """开一个事务。

        写事务用 ``BEGIN IMMEDIATE``：立刻拿写锁，拿不到就退避重试
        （**重试的是整个事务**，此时事务体尚未执行，所以等价于整事务重来，
        不会重放半条语句的副作用）。读事务用 ``BEGIN``（DEFERRED）。
        """
        conn = self._ensure_conn()
        if write:
            self._begin_immediate(conn)
        else:
            self._exec(conn, "BEGIN", op="begin/read")
        return _SqliteTxn(self, conn, write=write)

    # -- 连接管理 ---------------------------------------------------------
    def _connect_with_probe(self) -> sqlite3.Connection:
        """建立连接 + 设 PRAGMA + **可写性探针**（STORE-SCHEMA §6.2 第 3 条）。

        探针很有必要：把一条只读连接交给业务代码，它会在第一次 ``add`` 时才炸，
        而那时已经离故障点很远了（中间可能已经发出好几个 payload）。
        """
        last: BaseException | None = None
        for attempt in range(self._connect_attempts):
            conn = None
            try:
                conn = sqlite3.connect(
                    self.path,
                    isolation_level=None,          # 自己管事务，禁掉隐式 BEGIN
                    timeout=max(1.0, self._busy_timeout_ms / 1000.0),
                )
                conn.row_factory = sqlite3.Row
                # PRAGMA 必须在 BEGIN 之前（事务内 foreign_keys/journal_mode 会被静默忽略）
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = %d" % self._busy_timeout_ms)
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA user_version = user_version")   # no-op 写探针
                conn.execute("ROLLBACK")
                return conn
            except sqlite3.Error as exc:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                last = exc
                if _is_readonly(exc):
                    # 冷启动抢建 -shm 失败 → 该连接永久只读。换一条，重开几乎总能成功。
                    self.recycle_count += 1
                    time.sleep(0.01 * (attempt + 1))
                    continue
                if _is_busy(exc):
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise map_error(exc, op="open") from exc
        raise map_error(last or StoreError("无法打开数据库"), op="open")

    def _ensure_conn(self) -> sqlite3.Connection:
        """取一条可用连接；上次被判定为「只读」的连接在这里被换掉。

        **换连接的时机是「下一次新事务开始前」，不是「本次失败时」**（§6.3）：
        本次失败如实上报（``StoreIOError``），修复留给下一次调用。这样热路径
        既没有重试、也没有重放，故障不会被放大成「这个进程写不了盘」。
        """
        if self._conn is None or self._conn_dead:
            if self._conn_dead:
                self.recycle_count += 1
            self._discard_conn()
            self._conn = self._connect_with_probe()
            self._migrate(self._conn)
            self._ensure_rows(self._conn)
            self._conn_dead = False
            self._opened = True
        return self._conn

    def _discard_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._conn_dead = False

    def _begin_immediate(self, conn: sqlite3.Connection) -> None:
        """``BEGIN IMMEDIATE`` + **事务粒度**的忙等待。

        重试单元是「拿到写锁」这件事，不是某条语句：``BEGIN IMMEDIATE`` 失败时
        事务体一行都还没跑，所以重试等价于整个事务重来。
        """
        last: BaseException | None = None
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                last = exc
                if _is_readonly(exc):
                    self._conn_dead = True
                    raise map_error(exc, op="begin(write)") from exc
                if _is_busy(exc) and attempt < 2:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise map_error(exc, op="begin(write)") from exc
        raise map_error(last or StoreError("拿不到写锁"), op="begin(write)")

    # -- 初始化 -----------------------------------------------------------
    def _migrate(self, conn: sqlite3.Connection) -> None:
        """``PRAGMA user_version`` 驱动的迁移（STORE-SCHEMA §7）。

        ``v > CURRENT`` **拒绝打开**：旧代码读新库会静默忽略不认识的列、按旧语义
        写回，把新字段写丢。报错比「看起来能跑」安全得多。
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError(
                "库的 schema 版本 %d 高于本 SDK 支持的 %d：拒绝打开。"
                "旧代码读新库会静默丢字段（它只是不读不认识的列），"
                "请升级 SDK 或用新版本导出" % (version, SCHEMA_VERSION))
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            # executescript 会隐式提交，必须在事务之外调用。
            conn.executescript(DDL)
        # 一步一个事务：DDL + user_version 同事务提交，失败即整体回滚。
        self._exec(conn, "BEGIN IMMEDIATE", op="migrate")
        try:
            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            self._exec(conn, "COMMIT", op="migrate")
        except sqlite3.Error:
            self._exec(conn, "ROLLBACK", op="migrate")
            raise

    def _ensure_rows(self, conn: sqlite3.Connection) -> None:
        """建库种子（T5）：``store`` 一行 + ``session`` 一行 + ``num_alloc`` 游标。

        种子缺失时第一轮分配会撞 ``UNIQUE(store_id, num)``，而不是从 1 开始。
        """
        now = self._now()
        empty_hash = _body_hash_of(self.top_text, self.scope, ())
        self._exec(conn, "BEGIN IMMEDIATE", op="seed")
        try:
            conn.execute(
                "INSERT INTO store (id, scope, top_text, fmt_version, rev, body_hash,"
                " created_at, updated_at) VALUES (?,?,?,'1',0,?,?,?)"
                " ON CONFLICT(id) DO NOTHING",
                (self.store_id, self.scope, self.top_text, empty_hash, now, now))
            conn.execute(
                "INSERT INTO session (id, store_id, arm, transcript_hash, tokenizer,"
                " price_p, cache_delta, mode, turn_seq, config_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,0,'{}',?) ON CONFLICT(id) DO NOTHING",
                (self.session_id, self.store_id, self.arm, self.transcript_hash,
                 self.tokenizer, self.price_p, self.cache_delta, self.mode, now))
            conn.execute(
                "INSERT INTO num_alloc (store_id, kind, parent_num, last_num)"
                " VALUES (?,'T',0,0) ON CONFLICT(store_id, kind, parent_num) DO NOTHING",
                (self.store_id,))
            self._exec(conn, "COMMIT", op="seed")
        except sqlite3.Error as exc:
            if not _is_readonly(exc):
                self._exec(conn, "ROLLBACK", op="seed")
            raise map_error(exc, op="seed") from exc

    # -- 小工具 -----------------------------------------------------------
    def _exec(self, conn: sqlite3.Connection, sql: str, *, op: str) -> None:
        try:
            conn.execute(sql)
        except sqlite3.Error as exc:
            if _is_readonly(exc):
                self._conn_dead = True
            raise map_error(exc, op=op) from exc

    def _all(self, conn, sql, params, *, op: str) -> list[sqlite3.Row]:
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            if _is_readonly(exc):
                self._conn_dead = True
            raise map_error(exc, op=op) from exc

    def _one(self, conn, sql, params, *, op: str) -> sqlite3.Row | None:
        rows = self._all(conn, sql, params, op=op)
        return rows[0] if rows else None

    def _scalar(self, conn, sql, params, *, op: str):
        row = self._one(conn, sql, params, op=op)
        return None if row is None else row[0]

    def _load_topics(self, conn) -> tuple[Topic, ...]:
        """按 P5 的**唯一序**取整棵树：T 按 num、P 按 num、叶子按 id（插入序）。

        这条 ORDER BY 必须与 ``text.body_key`` 的前像同序 —— 两处一旦不一致，
        ``body_hash`` 就与实际渲染字节脱钩，而它们各自看起来都很确定。
        """
        rows = self._all(
            conn,
            "SELECT t.num AS t_num, t.name AS t_name, t.tag AS t_tag,"
            " p.num AS p_num, p.name AS p_name, p.tag AS p_tag,"
            " l.id AS l_id, l.kind AS l_kind, l.text AS l_text, l.tag AS l_tag,"
            " l.src AS l_src, l.nbytes AS l_nbytes, l.sha16 AS l_sha16"
            " FROM topic t"
            " LEFT JOIN purpose p ON p.topic_id = t.id AND p.state = 'live'"
            " LEFT JOIN leaf l ON l.purpose_id = p.id AND l.state = 'live'"
            " WHERE t.store_id = ? AND t.state = 'live'"
            " ORDER BY t.num, p.num, l.id",
            (self.store_id,), op="load_topics")

        topics: list[dict] = []
        cur_topic: dict | None = None
        cur_purpose: dict | None = None
        for row in rows:
            if cur_topic is None or cur_topic["num"] != row["t_num"]:
                # 换事物：目的游标必须一并清空，否则 P1 会挂到 T2 下面。
                cur_topic = {
                    "num": int(row["t_num"]), "name": row["t_name"],
                    "tag": row["t_tag"], "purposes": [],
                }
                topics.append(cur_topic)
                cur_purpose = None
            if row["p_num"] is not None and (
                    cur_purpose is None or cur_purpose["num"] != row["p_num"]):
                cur_purpose = {
                    "num": int(row["p_num"]), "name": row["p_name"],
                    "tag": row["p_tag"], "leaves": [],
                }
                cur_topic["purposes"].append(cur_purpose)
            if row["l_id"] is not None and cur_purpose is not None:
                cur_purpose["leaves"].append(Leaf(
                    kind=row["l_kind"], text=row["l_text"], tag=row["l_tag"],
                    src=row["l_src"], nbytes=row["l_nbytes"], sha16=row["l_sha16"]))

        return tuple(
            Topic(
                num=t["num"], name=t["name"], tag=t["tag"],
                purposes=tuple(
                    Purpose(num="%d.%d" % (t["num"], p["num"]), name=p["name"],
                            tag=p["tag"], leaves=tuple(p["leaves"]))
                    for p in t["purposes"]),
            )
            for t in topics
        )

    def _latest_plan(self, conn) -> AssemblyPlan | None:
        """``ORDER BY generation DESC LIMIT 1``（R7：不能用 ``MAX(epoch_turn)``）。"""
        row = self._one(
            conn,
            "SELECT * FROM plans WHERE session_id = ? ORDER BY generation DESC LIMIT 1",
            (self.session_id,), op="latest_plan")
        return None if row is None else _plan_from_row(row)

    def _latest_system(self, conn) -> SystemPrompt | None:
        row = self._one(
            conn,
            "SELECT text, sha16, source FROM system_prompt WHERE session_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (self.session_id,), op="latest_system")
        if row is None:
            return None
        return SystemPrompt(text=row["text"], sha16=row["sha16"], source=row["source"])

    def _now(self) -> str:
        return safe_iso(self._clock, "1970-01-01T00:00:00.000Z")


# ---------------------------------------------------------------------------
# 事务
# ---------------------------------------------------------------------------
class _SqliteTxn:
    """一个事务。``commit()`` 原子；``rollback()`` 丢弃全部未提交改动（P2）。"""

    def __init__(self, store: SqliteStore, conn: sqlite3.Connection, *, write: bool) -> None:
        self._store = store
        self._conn = conn
        self._write = write
        self._finished = False
        #: T2 的 no-op 分支：``body_hash`` 未变 → commit 时 ROLLBACK。
        #: 只读事务 + COMMIT 不写 WAL 页，效果等价，但 ROLLBACK 把「分配器也一并
        #: 回滚」这件事说得更明白（no-op 不该泄漏一个编号）。
        self._noop = False
        self._alloc: NumberAllocator | None = None
        self._topics_cache: list[Topic] | None = None

    # -- 上下文协议 -------------------------------------------------------
    def __enter__(self) -> "_SqliteTxn":
        return self

    def __exit__(self, et, ev, tb) -> bool:
        if not self._finished:
            self.rollback()
        return False

    # -- 写操作 -----------------------------------------------------------
    def _require_write(self, what: str) -> None:
        if not self._write:
            raise StoreError(
                "只读事务不能执行 %s：请用 begin(write=True) 开启写事务" % what)
        if self._finished:
            raise StoreError("%s：事务已结束" % what)

    def apply_delta(self, lines: Sequence[str]) -> MergeStat:
        """T2：并入键树。**``body_hash`` 相等即整事务回滚**（no-op 不写盘、不 +rev）。

        归并语义（同文本+新标记=改标记 / 纯重复 → r_dup / E 按 (摘要,src) 定位）
        由 ``attribution.apply_delta`` 单点实现；本方法只负责「把结果落成 SQL」
        与「算出 rev 该不该动」。
        """
        self._require_write("apply_delta")
        from . import attribution

        old_topics = tuple(self._load_topics())
        alloc = self._load_allocator()
        new_topics, stat = attribution.apply_delta(old_topics, attribution.parse_delta(lines),
                                                  alloc=alloc)
        head = self._store._one(
            self._conn, "SELECT top_text, scope, body_hash FROM store WHERE id = ?",
            (self._store.store_id,), op="apply_delta/store")
        if head is None:
            raise StoreCorruptError(
                "store 行不见了（store_id=%r）：外键级联或外部删除"
                % self._store.store_id)
        new_hash = _body_hash_of(head["top_text"], head["scope"], new_topics)
        if new_hash == head["body_hash"]:
            # 内容指纹未变 → 不写盘、不 +rev（P4）。分配器也随之作废。
            self._noop = True
            return replace(stat, changed=False)

        self._sync_tree(old_topics, new_topics)
        self._save_allocator(alloc)
        self._store._all(
            self._conn,
            "UPDATE store SET rev = rev + 1, body_hash = ?, updated_at = ? WHERE id = ?",
            (new_hash, self._store._now(), self._store.store_id), op="apply_delta/rev")
        self._topics_cache = list(new_topics)
        return stat

    def put_turn(
        self,
        turn: Turn,
        *,
        part_tokens: Sequence[int] | None = None,
    ) -> None:
        """T1：追加一轮（1 条 user + 其后若干条 assistant/tool）。

        **不拍平成"user/assistant 各一列"**：``Turn.replies`` 是**有序序列**，
        用 ``part`` 表达它的序（part 0 = user，1..n = replies）。所以 ``K`` 的单位
        是「轮」而不是「条消息」—— 否则窗口宽度会随工具调用条数漂移。

        分叉检测：同一 ``(turn_no, part)`` 内容不同 → ``SessionConflictError``。
        静默覆盖会让缓存命中率虚高（前一轮发的是 A，库里记的是 B）。

        **为什么不是 ``StoreCorruptError``**（与 ``store._MemoryTxn.put_turn``
        同一段理由，两处必须逐字同向）：这一支**被正确输入撞得到** —— 两个
        ``Session`` 各持一份缓冲写同一条轮次链（P9 被违反）时，各轮内容不同，
        先到的已落库，后到的必撞这里。那是调用方的进程模型错了，不是库坏了；
        译成 ``STORE_CORRUPT`` 会让运维去跑 ``integrity_check``（一定全绿），
        真正的处置"这个 session 有两个驱动"永远不会被执行。

        反面参照：同文件 ``put_plan`` 里"撞 ``plans`` 唯一键"**真阳性为 0**，
        所以那支根本不该存在。**同形状，相反结论，因为可达输入集相反。**

        与"跳号"的区别：跳号（``turn.index > turn_seq + 1``）在 SQLite 侧可由
        **外部改库**造成（``session.turn_seq`` 被外部回拨），所以它留
        ``StoreCorruptError``；而本支的前提是"同一轮的字节变了"，外部改库触发不到
        它 —— 外部改第 2 轮时，本会话正在写第 5 轮，``turn_no`` 不同。
        """
        self._require_write("put_turn")
        now = self._store._now()
        seq_row = self._store._one(
            self._conn, "SELECT turn_seq FROM session WHERE id = ?",
            (self._store.session_id,), op="put_turn/seq")
        if seq_row is None:
            raise StoreCorruptError(
                "session 行不见了（session_id=%r）" % self._store.session_id)
        turn_seq = int(seq_row["turn_seq"])
        if turn.index > turn_seq + 1:
            raise StoreCorruptError(
                "轮次跳号：已发号到 %d，本次要写第 %d 轮。轮次号必须连续 —— "
                "跳号会在 R 段留下一个洞，而洞是不可装配的" % (turn_seq, turn.index))

        messages = turn.messages()
        tokens = list(part_tokens) if part_tokens is not None else None
        existing = {
            int(r["part"]): (r["role"], r["content_hash"], int(r["pinned"]))
            for r in self._store._all(
                self._conn,
                "SELECT part, role, content_hash, pinned FROM turn"
                " WHERE session_id = ? AND turn_no = ?",
                (self._store.session_id, turn.index), op="put_turn/parts")
        }
        for part, message in enumerate(messages):
            content_hash = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
            pinned = 1 if (part == 0 and turn.pinned) else 0
            token_count = 0 if tokens is None else int(tokens[part])
            if part in existing:
                role0, hash0, pinned0 = existing[part]
                if hash0 != content_hash:
                    raise SessionConflictError(
                        "第 %d 轮 part=%d 的内容与已存记录不同（已存 %s，本次 %s）："
                        "一轮的内容落库后永不合法变化，这通常意味着同一 session 被"
                        "两个驱动写（P9 被违反）；静默覆盖会让缓存命中率虚高"
                        % (turn.index, part, hash0[:16], content_hash[:16]))
                if role0 != message.role.value:
                    raise SessionConflictError(
                        "第 %d 轮 part=%d 的角色与已存记录不同（%s → %s）"
                        % (turn.index, part, role0, message.role.value))
                if pinned0 != pinned:
                    self._store._all(
                        self._conn,
                        "UPDATE turn SET pinned = ? WHERE session_id = ?"
                        " AND turn_no = ? AND part = 0",
                        (pinned, self._store.session_id, turn.index),
                        op="put_turn/pinned")
                continue
            self._store._all(
                self._conn,
                "INSERT INTO turn (session_id, turn_no, part, role, content,"
                " content_hash, token_count, pinned, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (self._store.session_id, turn.index, part, message.role.value,
                 message.text, content_hash, token_count, pinned, now),
                op="put_turn/insert")

        if turn.index > turn_seq:
            self._store._all(
                self._conn, "UPDATE session SET turn_seq = ? WHERE id = ?",
                (turn.index, self._store.session_id), op="put_turn/seq_update")

    def put_plan(
        self,
        plan: AssemblyPlan,
        *,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
    ) -> int:
        """T3（只写 ``plans``）：按 generation 追加，**永不覆盖**（P8）。

        返回**实际分配到的 generation**（本方法在写事务内分配）。调用方**必须**
        把返回值采纳回内存里的 plan（``replace(plan, generation=…)``）——
        ``plan.generation`` 进到这里时只是**候选号**，只用来识别"这是一次重试"。

        ⚠️ 注解是 ``-> int`` 而不是 ``-> None``，这不是风格问题：下面的两个
        ``return`` 一旦被当成死代码删掉，压缩就会永远沿用候选号，两个驱动重新
        错开 —— 而这**不会有任何断言变红**（号错了照样能 INSERT，只是悄悄多了一代）。
        所以 ``tests/test_store_contract.py`` 里有一条用
        ``inspect.signature(...).return_annotation`` 钉住三处注解（协议 /
        内存版 / SQLite 版）的用例，还有一条行为级用例钉"同号同字节返原号、
        同号不同字节返更大号、两次都不抛"。

        ``tokens_before`` / ``tokens_after`` 是 DDL 的两个测量列，``AssemblyPlan``
        没有它们的载体 —— 走本方法的内部扩展参数，缺省时退化为 ``frozen_tokens``
        （测量粒度失真，但不会是 NULL/0 这种假数据）。

        **本方法不碰 ``store``**（I16）：压缩不改键树，因此不得让 ``rev`` 变化。
        """
        self._require_write("put_plan")
        before = int(plan.frozen_tokens if tokens_before is None else tokens_before)
        after = int(plan.frozen_tokens if tokens_after is None else tokens_after)
        # 逐条校验 `plans` 的 CHECK，先把"压缩算错了"拦在 SQLite 之外。
        #
        # 为什么值得前置：不前置的话，一条 `r_first_turn` 越界会以 `IntegrityError`
        # 的形式出现，而它和 UNIQUE 违反在**同一句 INSERT** 上长得一模一样，
        # 只能靠错误文本区分 —— 那是随 SQLite 版本变的东西（团队已裁：按调用点，不按文本）。
        _check_plan_row(plan, before, after)

        # ★ 号在**本事务内**分配（ARCHITECTURE §10.4）：`BEGIN IMMEDIATE` 从进方法起
        # 就握着写锁，所以 `MAX(generation)` 读到之后到 INSERT 之间不可能有第三方提交。
        #
        # 这一步把"两个驱动撞唯一键"从**概率性问题**变成**结构上不可能**：
        # 后来的那个看到的 MAX 已经含前一个，它自然拿到更大的号。
        # 这比"撞了再翻译成某个异常"强 —— 后者要求先撞上，而撞上与否取决于时序。
        head = self._store._one(
            self._conn,
            "SELECT generation, frozen_sha16, frozen_text FROM plans"
            " WHERE session_id = ? ORDER BY generation DESC LIMIT 1",
            (self._store.session_id,), op="put_plan/max_generation")
        latest = int(head["generation"]) if head is not None else 0

        # 重试：候选号就是最新号，且冻结字节逐字相同 → 不新增行，返回原号。
        # 判据用"字节相同"而不是"号相同"：号相同但字节不同，说明调用方拿着过期
        # snapshot 在写（两个驱动的形状），那不是重试，该给它一个新号。
        if (head is not None and plan.generation == latest
                and head["frozen_sha16"] == plan.frozen_sha16
                and head["frozen_text"] == plan.frozen_text):
            return latest

        allocated = latest + 1
        self._store._all(
            self._conn,
            "INSERT INTO plans (session_id, schema, generation, epoch_turn,"
            " r_first_turn, frozen_text, frozen_sha16, frozen_tokens, tokens_before,"
            " tokens_after, system_sha16, snapshot_id, store_rev, counter_id,"
            " policy_digest, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self._store.session_id, plan.schema, allocated, plan.epoch_turn,
             plan.r_first_turn, plan.frozen_text, plan.frozen_sha16, plan.frozen_tokens,
             before, after, plan.system_sha16, plan.snapshot_id, plan.store_rev,
             plan.counter_id, plan.policy_digest, self._store._now()),
            op="put_plan/commit")
        # 到这里仍撞 `UNIQUE(session_id, generation)` 的情形**不存在**：锁在本事务里，
        # MAX 已经含所有已提交的号。所以这里**故意没有** `except … → SessionConflictError`：
        # 一个真阳性为 0 的 except 不是防御，是伪装 —— 它会让人以为 P9 已经被检测到了。
        # 若将来去掉 `BEGIN IMMEDIATE`，这条推理必须重算，而不是把 except 加回来。
        return allocated

    def put_system(self, prompt: SystemPrompt) -> None:
        self._require_write("put_system")
        self._store._all(
            self._conn,
            "INSERT INTO system_prompt (session_id, sha16, text, source, created_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(session_id, sha16) DO NOTHING",
            (self._store.session_id, prompt.sha16, prompt.text, prompt.source,
             self._store._now()), op="put_system")

    def append_event(self, event: "CompactionEvent | DegradationRecord") -> None:
        """T3/T4：压缩事件与降级记录同表（``event``），只追加。"""
        self._require_write("append_event")
        now = self._store._now()
        if isinstance(event, CompactionEvent):
            self._store._all(
                self._conn,
                "INSERT INTO event (session_id, kind, at_turn, reason, generation,"
                " frozen_sha16, frozen_tokens, included_json, omitted_json, aborted,"
                " abort_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._store.session_id, "compaction", int(event.at_turn),
                 _reason_value(event.reason), int(event.generation),
                 event.frozen_sha16, int(event.frozen_tokens),
                 _json(event.included), _json(event.omitted),
                 1 if event.aborted else 0, event.abort_reason, now),
                op="append_event/compaction")
            return
        if isinstance(event, DegradationRecord):
            self._store._all(
                self._conn,
                "INSERT INTO event (session_id, kind, at_turn, reason, at_tick, detail,"
                " snapshot_id, fallback, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (self._store.session_id, "degradation", max(1, int(event.at_turn)),
                 _reason_value(event.reason), int(event.at_tick), event.detail,
                 event.snapshot_id, event.fallback, now),
                op="append_event/degradation")
            return
        raise StoreError(
            "append_event 只接受 CompactionEvent / DegradationRecord，收到 %s"
            % type(event).__name__)

    def put_measure(self, measure: Measure) -> None:
        """测点入库（``UNIQUE(session_id, turn_no, attempt)``：重试不虚增样本）。"""
        self._require_write("put_measure")
        self._store._all(
            self._conn,
            "INSERT INTO measure (session_id, turn_no, attempt, input_tokens,"
            " cached_tokens, output_tokens, payload_hash, system_hash, latency_ms, ok,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, turn_no, attempt) DO NOTHING",
            (measure.session_id or self._store.session_id, measure.turn_no,
             measure.attempt, measure.input_tokens, measure.cached_tokens,
             measure.output_tokens, measure.payload_hash, measure.system_hash,
             measure.latency_ms, 1 if measure.ok else 0, self._store._now()),
            op="put_measure")

    # -- 提交 / 回滚 ------------------------------------------------------
    def commit(self) -> None:
        if self._finished:
            raise StoreError("事务已结束（commit/rollback 只能成功调用一次）")
        if not self._write:
            self._store._exec(self._conn, "COMMIT", op="commit/read")
            self._finished = True
            return
        if self._noop:
            # no-op：整体回滚，连分配器一起（不泄漏编号、不写 WAL 页）。
            self._store._exec(self._conn, "ROLLBACK", op="commit/noop")
            self._finished = True
            return
        self._store._exec(self._conn, "COMMIT", op="commit")
        self._finished = True

    def rollback(self) -> None:
        if not self._finished:
            try:
                self._store._exec(self._conn, "ROLLBACK", op="rollback")
            finally:
                self._finished = True
        else:
            self._finished = True

    # -- 内部 -------------------------------------------------------------
    def _load_topics(self) -> tuple[Topic, ...]:
        if self._topics_cache is not None:
            return tuple(self._topics_cache)
        topics = self._store._load_topics(self._conn)
        self._topics_cache = list(topics)
        return topics

    def _load_allocator(self) -> "NumberAllocator":
        from . import attribution

        if self._alloc is not None:
            return self._alloc
        topic_last = 0
        purpose_last: dict[int, int] = {}
        for row in self._store._all(
                self._conn,
                "SELECT kind, parent_num, last_num FROM num_alloc WHERE store_id = ?",
                (self._store.store_id,), op="load_allocator"):
            if row["kind"] == "T":
                topic_last = int(row["last_num"])
            else:
                purpose_last[int(row["parent_num"])] = int(row["last_num"])
        self._alloc = attribution.NumberAllocator(
            topic_last=topic_last, purpose_last=purpose_last)
        return self._alloc

    def _save_allocator(self, alloc: "NumberAllocator") -> None:
        """分配器只增不减：``last_num = MAX(旧, 新)``。

        ``MAX(num)+1`` 不可替代：``MAX`` 的语义是「现存行里最大的号」，一旦某行
        被物理删除，它就会给出一个**曾经用过**的号，外部引用（笔记里的 T2、
        导出 JSON）静默指向另一个事物。
        """
        rows = [("T", 0, alloc.snapshot_topic_last())]
        for parent_num, last in sorted(alloc.snapshot_purpose_last().items()):
            rows.append(("P", int(parent_num), int(last)))
        for kind, parent_num, last in rows:
            self._store._all(
                self._conn,
                "INSERT INTO num_alloc (store_id, kind, parent_num, last_num)"
                " VALUES (?,?,?,?) ON CONFLICT(store_id, kind, parent_num)"
                " DO UPDATE SET last_num = MAX(num_alloc.last_num, excluded.last_num)",
                (self._store.store_id, kind, parent_num, last),
                op="save_allocator")

    def _sync_tree(self, old_topics: Sequence[Topic], new_topics: Sequence[Topic]) -> None:
        """把归并后的键树落成 SQL 增量。

        叶子用 upsert + ``COALESCE(excluded.tag, leaf.tag)``：两个方向错了都**静默**
        —— ``OR IGNORE`` 让「同文本 + 新标记」退化成 no-op（``[已废]`` 永远打不上），
        ``SET tag = excluded.tag`` 会把已有的 ``[钉住]`` 清成 NULL（
        用户明确钉住的内容被老化折叠掉，是静默数据丢失）。
        """
        now = self._store._now()
        old_topic_by_num = {t.num: t for t in old_topics}
        for topic in new_topics:
            old_topic = old_topic_by_num.get(topic.num)
            if old_topic is None:
                self._store._all(
                    self._conn,
                    "INSERT INTO topic (store_id, num, name, name_norm, tag, state,"
                    " created_at) VALUES (?,?,?,?,?,'live',?)",
                    (self._store.store_id, topic.num, topic.name,
                     normalize(topic.name), topic.tag, now), op="sync_tree/topic_insert")
                topic_id = int(self._store._scalar(
                    self._conn,
                    "SELECT id FROM topic WHERE store_id = ? AND num = ?",
                    (self._store.store_id, topic.num), op="sync_tree/topic_id"))
            else:
                topic_id = self._topic_id(topic.num)
                if old_topic.name != topic.name or old_topic.tag != topic.tag:
                    self._store._all(
                        self._conn,
                        "UPDATE topic SET name = ?, name_norm = ?, tag = ? WHERE id = ?",
                        (topic.name, normalize(topic.name), topic.tag, topic_id),
                        op="sync_tree/topic_update")
            self._sync_purposes(topic, topic_id, now)

    def _topic_id(self, num: int) -> int:
        value = self._store._scalar(
            self._conn, "SELECT id FROM topic WHERE store_id = ? AND num = ?",
            (self._store.store_id, num), op="topic_id")
        if value is None:
            raise StoreCorruptError(
                "topic num=%d 刚刚写入却查不到 id：库被外部改过" % num)
        return int(value)

    def _sync_purposes(self, topic: Topic, topic_id: int, now: str) -> None:
        for purpose in topic.purposes:
            seq = int(purpose.num.split(".")[-1])
            purpose_id = self._store._scalar(
                self._conn,
                "SELECT id FROM purpose WHERE topic_id = ? AND num = ?",
                (topic_id, seq), op="sync_tree/purpose_id")
            if purpose_id is None:
                self._store._all(
                    self._conn,
                    "INSERT INTO purpose (topic_id, num, name, name_norm, tag, state,"
                    " created_at) VALUES (?,?,?,?,?,'live',?)",
                    (topic_id, seq, purpose.name, normalize(purpose.name),
                     purpose.tag, now), op="sync_tree/purpose_insert")
                purpose_id = int(self._store._scalar(
                    self._conn,
                    "SELECT id FROM purpose WHERE topic_id = ? AND num = ?",
                    (topic_id, seq), op="sync_tree/purpose_id2"))
            else:
                row = self._store._one(
                    self._conn,
                    "SELECT name, tag FROM purpose WHERE id = ?", (purpose_id,),
                    op="sync_tree/purpose_row")
                if row is None or row["name"] != purpose.name or row["tag"] != purpose.tag:
                    self._store._all(
                        self._conn,
                        "UPDATE purpose SET name = ?, name_norm = ?, tag = ? WHERE id = ?",
                        (purpose.name, normalize(purpose.name), purpose.tag, purpose_id),
                        op="sync_tree/purpose_update")
            for leaf in purpose.leaves:
                self._upsert_leaf(purpose_id, leaf, now)

    def _upsert_leaf(self, purpose_id: int, leaf: Leaf, now: str) -> None:
        text_norm = normalize(leaf.text)
        if leaf.kind == "E":
            # E 的身份是 (摘要, src)：换源文件是新增，不是改内容。
            self._store._all(
                self._conn,
                "INSERT INTO leaf (purpose_id, kind, text, text_norm, tag, src, nbytes,"
                " sha16, state, created_at) VALUES (?,?,?,?,?,?,?,?,'live',?)"
                " ON CONFLICT(purpose_id, text_norm, src)"
                " WHERE kind = 'E' AND state = 'live'"
                " DO UPDATE SET tag = COALESCE(excluded.tag, leaf.tag),"
                " nbytes = excluded.nbytes, sha16 = excluded.sha16",
                (purpose_id, "E", leaf.text, text_norm, leaf.tag, leaf.src,
                 leaf.nbytes, leaf.sha16, now), op="sync_tree/leaf_e")
            return
        self._store._all(
            self._conn,
            "INSERT INTO leaf (purpose_id, kind, text, text_norm, tag, src, nbytes,"
            " sha16, state, created_at) VALUES (?,?,?,?,?,NULL,NULL,NULL,'live',?)"
            " ON CONFLICT(purpose_id, text_norm) WHERE kind = 'R' AND state = 'live'"
            " DO UPDATE SET tag = COALESCE(excluded.tag, leaf.tag)",
            (purpose_id, "R", leaf.text, text_norm, leaf.tag, now),
            op="sync_tree/leaf_r")


# ---------------------------------------------------------------------------
# 行 ↔ 模型
# ---------------------------------------------------------------------------
def _plan_from_row(row: sqlite3.Row) -> AssemblyPlan:
    return AssemblyPlan(
        schema=int(row["schema"]),
        generation=int(row["generation"]),
        epoch_turn=int(row["epoch_turn"]),
        r_first_turn=int(row["r_first_turn"]),
        frozen_text=row["frozen_text"],
        frozen_sha16=row["frozen_sha16"],
        frozen_tokens=int(row["frozen_tokens"]),
        system_sha16=row["system_sha16"],
        snapshot_id=row["snapshot_id"],
        store_rev=int(row["store_rev"]),
        counter_id=row["counter_id"],
        policy_digest=row["policy_digest"],
    )


def _reason_value(reason) -> str:
    return getattr(reason, "value", str(reason))


def _check_plan_row(plan: AssemblyPlan, tokens_before: int, tokens_after: int) -> None:
    """把 ``plans`` 表的 CHECK 在**插入之前**逐条过一遍 → ``InvariantError``。

    为什么必须前置：``put_plan`` 里唯一**可能由正确输入触发**的约束违反是
    ``UNIQUE(session_id, generation)``。CHECK 违反是**压缩算错了** —— 同一个压缩器
    下次还会算错，它不是调用方的错。

    为什么译 ``InvariantError`` 而不是 ``StoreCorruptError``：后者会让运维去跑
    ``PRAGMA integrity_check``，而那个结果一定是全绿（库确实是好的），于是排查方向
    被引到「库坏了」这条死路。前者指向**实现**：这个进程产出了一个自相矛盾的 plan。

    为什么必须在插入前挡掉、而不是靠后面那句 ``except``：同一句 INSERT 上 CHECK 与
    UNIQUE 都表现为 ``IntegrityError``，事后再分类只能靠消息文本，而那是随
    SQLite 版本变的东西（团队已裁定：按调用点，不按文本）。前置校验之后，能走到
    INSERT 还撞约束的只剩「号不在本事务内分配」这一类实现缺陷。

    这里逐字对齐 STORE-SCHEMA 的 DDL（含 ``r_first_turn <= epoch_turn + 1``），
    报错信息写清"哪个字段、什么值、应该是什么"，因为这一类错全是代码 bug，
    定位成本应该为零。
    """
    def bad(field: str, value: object, why: str) -> None:
        raise InvariantError(
            "压缩产出违反 plans 表约束（实现缺陷，不是并发、也不是库损坏）："
            "%s = %r %s" % (field, value, why))

    if plan.schema < 1:
        bad("schema", plan.schema, "必须 ≥ 1")
    # ``plan.generation`` **不校验**：它是候选号，真实号由 ``put_plan`` 在写事务内分配
    # （ARCHITECTURE §10.4）。校验一个不参与写库的字段只会造出"看起来在防什么"的假闸门，
    # 而 DDL 上那条 ``CHECK (generation >= 1)`` 作用在**分配后**的号上，永远成立。
    if plan.epoch_turn < 1:
        bad("epoch_turn", plan.epoch_turn, "必须 ≥ 1")
    if plan.r_first_turn < 1:
        bad("r_first_turn", plan.r_first_turn, "必须 ≥ 1")
    if plan.r_first_turn > plan.epoch_turn + 1:
        bad("r_first_turn", plan.r_first_turn,
            "不得超过 epoch_turn + 1 = %d（R 段不能从边界之后起步）"
            % (plan.epoch_turn + 1))
    if len(plan.frozen_text) < 1:
        bad("frozen_text", "<空>", "长度必须 ≥ 1：空冻结块没有可装配的字节")
    if len(plan.frozen_sha16) != 16 or any(c not in "0123456789abcdef"
                                           for c in plan.frozen_sha16):
        bad("frozen_sha16", plan.frozen_sha16, "必须是 16 位小写十六进制")
    if plan.frozen_tokens < 0:
        bad("frozen_tokens", plan.frozen_tokens, "必须 ≥ 0")
    if tokens_before < 0:
        bad("tokens_before", tokens_before, "必须 ≥ 0")
    if tokens_after < 0:
        bad("tokens_after", tokens_after, "必须 ≥ 0")
    if len(plan.system_sha16) != 16:
        bad("system_sha16", plan.system_sha16, "必须是 16 位")
    if len(plan.snapshot_id) != 16:
        bad("snapshot_id", plan.snapshot_id, "必须是 16 位")
    if plan.store_rev < 0:
        bad("store_rev", plan.store_rev, "必须 ≥ 0")

    # —— 下面两条原先漏了（`plans` 表上它们是 `NOT NULL`，本函数却一句都没问）——
    #
    # 为什么必须补：这两列不只是记账，它们**每轮都参与边界判定**
    # （``session._decide_boundary`` 的 R3 / R4）：
    #
    #   R3  ``policy_digest != policy.digest()``  → PLAN_INVALID
    #   R4  ``counter_id    != counter.counter_id`` → PLAN_INVALID
    #
    # 于是一个空串的代价不是"报告里少一列"，而是**每一轮都判 PLAN_INVALID → 每轮重压
    # → 每轮全价轮**，而且**不报任何错**（这正是 A7 那段「拿字段当闸门会让每轮重压」
    # 描述的失效形状，只不过这次是空值自己造出来的闸门）。
    #
    # 而 ``None`` 的代价是另一条：NOT NULL 在 INSERT 时才炸，被 ``map_error`` 映射成
    # ``StoreCorruptError`` → 运维去跑 ``integrity_check``（一定全绿），真正的处置
    # 「压缩器产出了一个残缺的 plan」永远不会被执行。
    # 判据：**不可达的 except 不是防御，是伪装** —— 它会让人以为那一层已经防住了。
    #
    # 所以两列都前置校验，且**空串也要拒**：空串能静默插进库，比 None 更贵。
    if not isinstance(plan.counter_id, str) or not plan.counter_id.strip():
        bad("counter_id", plan.counter_id,
            "必须是非空字符串（它是计费口径的标识；空值会让 R4 每轮判失效 → 每轮重压）")
    # ``Policy.digest()`` 是 ``sha256(...)[:16]``，所以长度与字符集都能钉死。
    if len(plan.policy_digest) != 16 or any(c not in "0123456789abcdef"
                                            for c in plan.policy_digest):
        bad("policy_digest", plan.policy_digest,
            "必须是 16 位小写十六进制（= sha256(policy)[:16]）；"
            "空值会让 R3 每轮判失效 → 每轮重压，且不报错")

    # 派生自洽：``frozen_sha16`` 必须是 ``frozen_text`` 的指纹。
    #
    # DDL 表达不了这条（它管不到跨列派生），但它的失效形状与上面两条**完全一样**：
    # 读侧 R1（``sha16_text(plan.frozen_text) != plan.frozen_sha16``）会判 PLAN_INVALID
    # → 每轮重压、每轮全价轮、不报错。
    #
    # 放在这里而不是只靠 R1：R1 是**读时**发现，代价已经付出去了（那一轮按旧 plan
    # 装配、再判失效）；这里能在**写时**就拦掉，报错指向产出这份 plan 的压缩器。
    if sha16_text(plan.frozen_text) != plan.frozen_sha16:
        bad("frozen_sha16", plan.frozen_sha16,
            "与 frozen_text 的指纹不符（实际 %s）：R1 会在读侧每轮判 PLAN_INVALID，"
            "于是每轮重压、每轮全价轮，而全程不报错"
            % sha16_text(plan.frozen_text))


def _json(mapping: Mapping[str, int]) -> str:
    """确定性 JSON：键排序、无空格、不转义非 ASCII（库是给人看的，也该是稳定的）。"""
    return json.dumps(dict(sorted(mapping.items())), ensure_ascii=False,
                      separators=(",", ":"))


def _warn_if_sync_folder(path: str) -> None:
    lowered = path.replace("\\", "/").lower()
    for marker in _SYNC_MARKERS:
        if marker in lowered:
            import sys

            print(
                "[ctxlattice] 警告：库文件位于同步盘/网络盘（命中 %r）：%s\n"
                "  WAL 的 -shm 依赖 mmap 与文件锁语义，同步盘会把偶发只读变成常态，"
                "典型症状是「单机跑得好好的、换台机器就丢数据」。" % (marker, path),
                file=sys.stderr)
            return


__all__ = ["DDL", "SCHEMA_VERSION", "SqliteStore", "map_error"]
