# STORE-SCHEMA —— C1 上下文存储的 SQLite 存储层

> 取代 `.ctx` 单文件整重写。**SQLite 是唯一真相，`.ctx` 降级为「投影」**：
> `render()` 必须逐字节复现原格式，以便 `validate` 与旧工具仍能读；但写路径只走库。
> 口径依赖：[MEASUREMENT-SPEC.md](./MEASUREMENT-SPEC.md)（计费与两臂）、[TRANSCRIPT-FORMAT.md](./TRANSCRIPT-FORMAT.md)（轮次与 role）。
> 语义依赖：`context-lattice/references/store-format.md` 的 V1–V20 不变量与 `ctx.py` 的 `parse/render/apply_delta/_body_key`。

本文件只写 DDL 与设计说明，不含实现代码。

---

## 一、表清单

| # | 表 | 一行是什么 | 回答什么问题 |
|---|---|---|---|
| 1 | `store` | 一个库（一个作用域的键树根） | rev / 内容指纹 / top 目标 / scope |
| 2 | `num_alloc` | 编号分配器游标 | 下一个 T / P 号（保证不复用） |
| 3 | `topic` | `T` 事物节点 | 上层键 |
| 4 | `purpose` | `P` 目的节点 | 下层键 |
| 5 | `leaf` | `R` 要求 **或** `E` 事件节点 | 叶子链（进窗口 / 只留指针） |
| 6 | `session` | 一次会话 = bench 的一臂一次跑 | arm / transcript 哈希 / 计费参数 |
| 7 | `turn` | 一轮里的一条消息 | 最近 K 轮原文 |
| 8 | `plans` | 一个装配计划（= 压缩边界 + 冻结块） | F 的逐字内容 / 冻结时指纹 / 边界前后 token |
| 9 | `system_prompt` | 一条 system 提示正文 | 两臂 system 是否逐字节相同 |
| 10 | `event` | 一条流水（压缩事件 / 降级记录） | 边界为何触发、失败如何降级 |
| 11 | `measure` | 一次请求的测点 | input / cached / output |

视图 4 个：`v_measure`（含 arm 与 `cost_eq`）、`v_turn_boundary`（轮次 × 是否在冻结窗口内）、`v_session_stats`（对应 `SessionStats`）、`v_key_counts`（头部计数）。

8/9/10 三张表不是原任务书里的，而是 ARCHITECTURE.md §4.5 的 `put_plan` / `put_system` / `append_event` 逼出来的产物——契约定了就必须有落点。其中 `plans` 取代了任务书里的「压缩边界表」：边界只是 plan 的一个字段（`epoch_turn`），单为边界建表会让「边界」和「冻结块」变成两份可互相矛盾的真相（详见第三节）。

**刻意不建的**：`atom`（内嵌原文副本）——副本路径由 `sha16` 完全决定（`<库>.atoms/<sha16>`），建表只会多一份会与文件系统不一致的真相；`tag` 字典表——标记按格式允许自由文本，字典表会把合法数据判成非法；变更历史表——本期不承诺审计链，见第九节；`stats` 表——`SessionStats` 全部可从 `turn`/`event`/`measure`/`plans` 现算（见 `v_session_stats`），落冗余列必然漂。

---

## 二、完整 DDL

按顺序执行即可（`sqlite3.executescript`）。**末尾不设 `user_version`**，由迁移器统一管（见第七节）。

```sql
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
```

---

## 三、逐表说明（约束与索引的理由）

### `store`
**用途**：一个键树的根，同时持有该树的 `rev` 与 `body_hash`。
**关键约束**：`scope` 是封闭枚举 + 可选限定名，形状用 `CHECK` 钉住（`user|proj|task`，第 5 位必须是 `:` 或 `/`）——对应 V15：写错成 `project`、`用户级` 必须当场拒绝，而不是当成未知值悄悄写进去。限定名的字符集与长度在应用层判，`CHECK` 只兜结构（SQLite 无正则）。
**为什么不把 `rev` 放 `session`**：`rev` 是**内容**的属性，跨会话累计；放会话里会变成「每会话一个 rev」，再也答不出「这份库改过几次」。

### `num_alloc`
**用途**：编号分配游标，`(store, kind, parent_num)` 三元主键，`WITHOUT ROWID` 少一层间接。
**为什么不能靠 `MAX(num)+1`**：`MAX` 的语义是「现存行里最大的号」，它依赖行的**存活**。一旦某行被物理删除（`prune --drop-stale` 折掉 `[已废]`、将来任何清理作业），`MAX` 就会给出一个**曾经用过**的号。而格式的硬约束是「编号一经分配不复用」——复用的后果是外部引用（索引文件、导出 JSON、人的笔记里的 `T2`）静默指向另一个事物，**没有任何报错**。分配器的存在就是把这条件写成结构：`last_num` 只增。
**可修复性**：分配器是派生量，与真实行不一致时可用 `last_num = MAX(num)` 重建（见第十节），因此它不是第二份真相，是**只增的缓存**。

### `topic` / `purpose`
**用途**：键层两级。`num` 是**身份**（对外引用），`name` 是**显示**，`name_norm` 是**判等**。
**为什么 `name` 与 `name_norm` 两列不是冗余**：`apply_delta` 用「折叠空白」后的文本判等，但 `render` 必须输出原文本。只留一列，要么渲染字节变了（违反确定性），要么判等语义变了（把 `a  b` 当新条目，重复入库）。导入手写 `.ctx` 时两者一定不同。
**关键约束**：`UNIQUE(store_id,num)` / `UNIQUE(topic_id,num)` 就是 V9；`state` 引入软删（`live|retired`），键层行**永不物理删除**，所以「已废的 T2」仍然占着 2 号。
**索引与理由**：
- `UNIQUE(store_id, num)` —— V9，同时是分配器的兜底（并发下两进程抢到同一号时这里报冲突，而不是写出两行同名 T）。
- `ix_topic_name(store_id, name_norm)` / `ix_purpose_name(topic_id, name_norm)` —— 幂等归并的查找路径。没有它，每次 `add` 都要全表扫一遍名字，O(叶子) 变 O(键树×增量行)。
- **名字索引刻意不唯一**：格式只要求编号唯一，一个手写库完全可以有两个同名目的。加唯一约束会让**合法库导入失败**——数据库比格式更严是 bug，不是安全。

### `leaf`
**用途**：`R` 与 `E` 同表。`kind` 白名单 `('R','E')` 把 `_kind()` 那条「未知种类必须报错，绝不默认当 R」的不变量**下沉成结构不可能**：写不进第三种 kind，就没有「被静默当 R 处理」的机会。
**关键约束**：
- `CHECK ((kind = 'E') = (src IS NOT NULL AND nbytes IS NOT NULL AND sha16 IS NOT NULL))` —— 一行要么是完整的 E（指针三元组齐全），要么是完整的 R（三元组全空）。半截事件行（有摘要无哈希）在任何写入路径都落不了库，对应 V19。
- `CHECK (kind = 'R' OR instr(text,'|') = 0)` —— V18：摘要里的半角竖线会切断 ` | src:` 元信息。
- `length(sha16)=16 AND sha16 NOT GLOB '*[^0-9a-f]*'` —— 16 位小写十六进制，V19。
- `text`/`tag` 的方括号与制表符禁令 —— V12 / V11。
**索引与理由**（四个不多）：
1. `ux_leaf_r(purpose_id, text_norm) WHERE kind='R' AND state='live'` —— **幂等归并的结构化**。两个进程同时 `add` 同一条要求时，不是「谁后写谁算数」，而是后者撞唯一索引，写路径据此走改标记分支。它兼任查重索引，不额外花钱。
2. `ux_leaf_e(purpose_id, text_norm, src) WHERE kind='E' AND state='live'` —— 同上，E 的身份是 `(摘要, src)`：`apply_delta` 就是按这两项匹配的，换 `src` 算新增。
3. `ix_leaf_purpose(purpose_id, id)` —— 渲染与计数按 `(purpose, 插入序)` 取活叶子。**索引顺序即输出顺序**，`ORDER BY` 不需要临时排序，确定性也不依赖优化器的临时表策略。
4. `ix_leaf_lifecycle(tag) WHERE state='live' AND tag IS NOT NULL` —— 老化只问「本库有哪些 `暂态`/`钉住`」，partial 索引体积 ∝ 有标记的叶子数，而不是整个叶子表。
**两个 partial 索引为什么必须带 `state='live'`**：退役行要留作审计，同一条要求可以「退役一次、再新增一次」。不加 `state` 条件，第二次新增会撞唯一索引——审计留痕与幂等归并就打起来了。

### `session`
**用途**：一次会话 = bench 的**一臂一次跑**。持 `arm`、`transcript_hash`、计费参数、`turn_seq`。
**为什么 `arm` 不放 `measure`（与任务书的一处刻意偏离）**：`arm` 在会话内恒定。放在两处，就会出现「`measure.arm` 与 `session.arm` 不一致」的行，而两个不同的 `GROUP BY` 各自读到一套——报告口径分叉，且**两者都算得出来、都不报错**。所以 `arm` 只放 `session`，`measure` 侧用视图 `v_measure` 暴露同名字段，调用方无感。
**为什么计费参数进库**：`cost_eq = billable×p + cached×p×δ`。`p`、`δ` 是参数不是结论，但**参数不入库，报告就无法被他人从库里复算**——MEASUREMENT-SPEC.md §3 要求打印所用 `δ/p`、MEASUREMENT-SPEC.md §4 要求「任何人可复算」，落库才做得到。`tokenizer` 同理：换分词器版本，绝对值就不可比。
**`turn_seq` 是分配器还是统计**：分配器。它是轮次号的唯一发号口，与 `turn` 的插入同事务（第六节 T1）。派生统计（总轮数、总 token）一律现算，不落列——落列就会漂。
**约束/索引**：`arm IN ('A','B')` 对齐 MEASUREMENT-SPEC 的两臂定义（加第三臂必须走迁移，这是有意的：口径变更必须留痕）；`transcript_hash` 让「两臂输入完全相同」成为一条可执行断言，而不是一句承诺；`ix_session_arm(arm, transcript_hash)` 正是那条断言的查询路径，兼作聚合分组索引。

### `turn`
**用途**：装配「最近 K 轮原文」的唯一来源。
**为什么需要 `part`**：TRANSCRIPT-FORMAT 规定「一轮 `user` 之后的所有 `assistant`/`tool` 行都属于同一轮」。用 `(turn_no, part)` 而不是给每条消息发新 `turn_no`，才能让「最近 K 轮」这个装配参数与「K 条消息」区分开——否则 K 的单位会随工具调用条数漂移。
**为什么 `role` 是封闭枚举 `user/assistant/tool`**：transcript 里没有 `system`（system 是静态段，不是轮次），多出 `system` 就说明有人把非对话内容塞进了轮次表——那会污染「最近 K 轮」的窗口。新增 role 需走迁移。
**为什么存 `content_hash`**：同一 `(session, turn_no, part)` 重放时，幂等键会让重复插入失败；但**内容不同**的冲突才是危险情形——上游分叉或纠正。此时必须报错（缓存前缀已变，静默覆盖会让缓存命中率虚高），所以要有 `content_hash` 才判得出「是重放还是分叉」。
**`replies` 是有序序列，不是拍平的单列**：`ARCHITECTURE.Turn` 是 `(index, user_text, replies: tuple[Message,...], pinned)`。本表用 `part` 表达 `replies` 的序——`part=0` 是 `user_text`，`part=1..n` 依次是 replies。两条 `CHECK` 把形状钉死在结构上：`part>0 ⇒ role='user'` 与 `part=0 ⇒ role∈{assistant,tool}`，合起来保证「一轮恰好一条 user，其余全是回复」，写不进「两个 user 一轮」或「user 混在回复中间」。`read_turns(lo, hi)` 的组装规则就是：按 `(turn_no, part)` 升序取，`part=0` 填 `user_text`，其余按序填 `replies`。
**`pinned` 落在行上，`Turn.pinned` 由行聚合而来**：transcript 的 `pinned` 是按行标的，而 `Turn.pinned` 只有一个布尔。我不想让写入方为了「后到的 reply 也标了 pinned」回头 UPDATE `part=0`——那会破坏只追加。所以定义 `Turn.pinned = EXISTS(SELECT 1 FROM turn WHERE session_id=? AND turn_no=? AND pinned=1)`。**能推的不要存**；存成「每行都要相同的冗余列」是引入一个可漂移的字段，而 `OR` 聚合不可能漂。
**索引**：只有 `UNIQUE(session_id, turn_no, part)`，一个够了——「最近 K 轮倒序」「边界之后所有轮」「全链复算」「P3 连续性断言」全部是它的前缀或有序扫描。**刻意不建** `(session_id, content_hash)`：没有一条读路径按内容查找，那会是一条只增写不减速的索引。

### `plans`
**用途**：一次压缩的**全部产物**——边界（`epoch_turn`）、冻结块正文（`frozen_text`）、冻结时的指纹（`frozen_sha16` / `snapshot_id` / `store_rev`）、token 记账（`tokens_before` / `tokens_after` / `frozen_tokens`）。
**为什么 `plans` 取代了任务书里的「压缩边界表」**：任务书要一张只记边界的表，ARCHITECTURE P8 要冻结块正文必须落库。若两表分开，「边界存在、冻结块还没写」就成了一种合法状态——而它恰好是 R7 里最致命的形态（该区间内容既不在窗口、也不在冻结块）。合成一张表，`frozen_text` 是 `NOT NULL`，那个状态在结构上就不存在。
**为什么按 `generation` 唯一，而不是按 `epoch_turn` 唯一**：这是对初稿的一处**纠正**。初稿写的是 `UNIQUE(session_id, turn_no)`，出发点是把「重试压缩」的幂等钉死；但 P8 明确要求 `plans` 按 `generation` 追加不覆盖、`snapshot()` 取最大 generation——也就是**允许同一个 `epoch_turn` 被压多次**（冻结块被判定损坏后按 PLAN_INVALID 重建）。按 `epoch_turn` 唯一会让这种重建直接被数据库挡掉，上层只能改元数据绕过约束，那是更坏的结果。幂等改由 `UNIQUE(session_id, generation)` 承担：重试提交同一个 generation 才是真正的「重复」，重压是新 generation。
**`frozen_sha16` 是 16 位而不是 64 位**：与 `leaf.sha16`、`AssemblyPlan.frozen_sha16` 同口径。代价是校验空间只有 64 bit——但它的用途是「抓到落库内容被改坏」，不是抗恶意构造，与 `E` 行取 16 位的理由完全一致。
**索引与理由**：`UNIQUE(session_id, generation)` 既是「取当前 plan（`ORDER BY generation DESC LIMIT 1`）」的索引，又是重试幂等闸门；`ix_plans_frozen(frozen_sha16)` 用于跨会话复用冻结块（同哈希只存一份）与「这段是否已压过」的快速判定。
**`snapshot_id` 与 `store_rev` 是审计字段，不是有效性闸门**（ARCHITECTURE.md §9.1）。这是整套设计里最容易埋回去的一颗雷：边界之间每调一次 `attribute()`，`store.rev` 就会 +1，于是「`plan.store_rev != 当前 rev`」在**几乎每一轮**都成立。若有人拿它当 `PLAN_INVALID` 判据，结果是**每轮重压、每轮全价轮**——不报错、不崩溃，只是成本静默翻几倍。F 的意义恰恰是冻结在过去的某个内容状态上，「与当前不一致」是设计意图。所以：这两个字段只写不判，存储层也不得提供 `is_plan_stale()` 之类带「是否有效」语义的便利方法。

**`snapshot_id` 的取值是派生量，不是现算的第三份哈希**：`sha256("ctxsnap1" \x1f schema \x1f system_sha16 \x1f frozen_sha16)[:16]`，输入全在本行里，写入时算一次即可（4.4）。因此它**不可能**与 `frozen_sha16` / `system_sha16` 打架——这是把「两个哈希会不会不一致」从「要小心维护」降级为「结构上不存在」。

**`commit_plan` 里禁止 `UPDATE store SET rev = rev + 1`。** 那一行是最自然的写法（"我写了一条新记录"）且完全说得通，但它会把 `rev` 从「键树改过几次」变成「做过几次操作」，此后所有 no-op 判定连锁失效——见 I16（4.6）。

**校验只针对最大 `generation` 那一行**（A7 第 4 个实例）：全表校验会让每个旧 generation 都报 `StoreCorruptError`，而库是健康的——**「旧行的 `snapshot_id` 与当前不一致」是设计意图，不是损坏**。旧行只受 `frozen_sha16` 自洽约束（`PV1`）。
**刻意不存 `included/omitted` 记账**：那是 `event` 的职责（见下），plan 只负责「装配时逐字要用的东西」。装配路径读 plan，报告路径读 event——两条路径各自只读一张表，不会互相拖累。

### `system_prompt`
**用途**：留住 system 提示的**正文**，使 `system_sha16` 可被复核。
**为什么正文也要存**：MEASUREMENT-SPEC.md §2 要求「两臂 system 逐字节相同」，MEASUREMENT-SPEC.md §4 要求「任何人可复算」。只存哈希，就只能比两个数相不相等，无法回答「那到底是哪段话」；一旦发现不等，连 diff 都做不出来。
**约束**：`UNIQUE(session_id, sha16)` —— 同会话同内容只存一份（重试幂等）；不同内容按行追加，`snapshot()` 取 `id` 最大的一条。

### `event`
**用途**：压缩事件与降级记录的合并流水。报告里「为什么在这一轮压缩」「失败时降级到什么」的唯一事实来源，也是 `SessionStats.boundaries / degradations` 的算料。
**为什么两类事件同表**：它们共享 `(session_id, at_turn, reason)` 三要素，且报告总是按轮次混排展示。分两张表就要 `UNION ALL` 才能按序输出，而那次排序还得再引入一个跨表的序——那是确定性的缝。
**关键约束**：`kind` 判别 + 两组必填 `CHECK`（compaction 必须有 `generation`/`frozen_sha16`/`included_json`/`omitted_json`；degradation 必须有 `at_tick`/`detail`/`fallback`）。半截事件在结构上落不了库，与 `leaf` 的 `kind` 判据同一个套路。`aborted=1` 必须带 `abort_reason`——「压了但失败了」是最容易只留一个 flag 就完事的状态，没有原因就无法归因。
**`at_tick` 是逻辑时钟**（`DegradationRecord` 原文），不是墙钟：墙钟进了库，同一份 transcript 两次回放的事件顺序就可能不同，而报告要逐字节可复现。

### `measure`
**用途**：逐轮测点。字段对齐 MEASUREMENT-SPEC.md §3 的 `gross / cached`，`billable` 与 `cost_eq` 交给 `v_measure`。
**关键约束**：
- `cached_tokens <= input_tokens` —— 防 bench 最常见的一种错：把缓存命中既当「已发」又当「另算」，`billable` 变负或双计。口径一旦错，后面所有比例都是错的，所以让它落不进库。
- `UNIQUE(session_id, turn_no, attempt)` —— 重试/超时重放不虚增样本。没有它，重试 3 次的轮次在均值里权重是 3。
- `ok` 必须落库（失败也写一行）——只记成功的样本会得到**幸存者偏差**：失败的那几轮恰恰通常是 payload 最大、最容易被截断的轮次，剔掉它们等于给自己发奖。
**`payload_hash` / `system_hash`**：不参与计费，只用于把「两臂差异只在历史段」「system 逐字节相同」从散文变成一条可执行查询（审计用）。

---

## 三之二、`StorePort` 契约对照（ARCHITECTURE.md §4.5 的 P1–P10）

架构文档说「宋砚秋的 schema 必须能兑现这些」。逐条给出**落点**，以及**兑现不了 / 需要改口径**的地方：

| # | 契约 | 本 schema 的落点 | 判定 |
|---|---|---|---|
| P1 | `snapshot()` 一致快照，绝无半个事务的中间态 | 读路径一律 `BEGIN`（DEFERRED）+ 多 SELECT + `COMMIT`（第五节、R1）；WAL 下读者不阻塞写者 | ✅ 兑现 |
| P2 | `commit()` 原子，ENOSPC 整体回滚不留半条 | SQLite 事务原生保证；`SQLITE_FULL` 须映射为 `StoreIOError` | ✅ 兑现（映射责任在 `store_sqlite.py`） |
| P3 | `read_turns` 轮次连续无洞，有洞即 `StoreCorruptError` | **表不强制**（见下），由读取路径断言 + 自检 SQL ⑦ | ⚠️ 只能应用层保证 |
| P4 | `rev` 只在 `body_key` 真变化时 +1 | 第四节：`rev` 是 `body_hash` 的函数；T2 的 `ROLLBACK` 分支 | ✅ 兑现 |
| P5 | 遍历序恒为 `(topic.num, purpose.num, 插入序)` | 4.3 与 4.2 前像同序（T 按 `num`、P 按 `topic.num`+`num`、叶子按 `id`） | ✅ 兑现（已按此条**修正**初稿的 `ORDER BY id`） |
| P6 | `snapshot_id = sha256("ctxsnap1" \x1f schema \x1f system_sha16 \x1f frozen_sha16)[:16]`，只覆盖 S+F 的字节来源 | 4.4：派生自两个权威哈希，不另算 dump；自动排除键树 / `rev` / rowid / 时间戳 / `turn` 边界 / 历史 `generation` / `policy_digest` | ✅ 兑现（**纯审计字段**，A7：不作有效性闸门） |
| P7 | **并发写不丢更新**：不同内容不丢更新；相同内容归并为一行；**不做**跨进程语义合并（行级后写者胜出） | `BEGIN IMMEDIATE` 串行 + partial unique 索引幂等归并 + 单语句原子发号（`num_alloc … RETURNING`）+ `turn_seq` 同事务；`readonly` 连接回收保证故障不永久放大（6.2 / 6.3） | ✅ 兑现（口径已由 team-lead 拍板改强） |
| P8 | `plans` 按 `generation` 追加不覆盖，存 `frozen_text`，`frozen_sha16` 校验 | `plans` 表：`UNIQUE(session_id, generation)`、`frozen_text TEXT NOT NULL`（不压 BLOB、不外链）、`frozen_sha16` 16 位、只取最大 generation、不一致即 `StoreCorruptError` 不静默修 | ✅ 兑现（已按此条**修正**初稿的「边界唯一」） |
| P9 | **同一 `session_id` 的装配驱动必须单进程**；不同 session 可并发写同一库 | 存储层不阻止，但**不提供**跨进程共享同一会话的语义：`turn`/`plans` 只按 `session_id` 追加，两个进程各驱同一 session 会各持一份内存缓冲 → 同一 session 产出不同 payload。库层面无冲突，会话层面是错的（见 R10） | ⚠️ 存储层可支持，**契约上禁止**；`stateless=True` 留作将来 |
| P10 | `read_turn_hashes(lo, hi)` 与 `read_turns(lo, hi)` **必须来自同一快照** | `turn.content_hash`（64 hex，本就有，不加列不加表）；两个 SELECT 都走 `UNIQUE(session_id, turn_no, part)` 的隐式索引，`EXPLAIN QUERY PLAN` 实测一条 COVERING、一条普通，**不需要为 P10 建新索引**。同快照的落地方式见下 | ⚠️ 存储层能做到，**端口形状做不到**（见下） |

五处需要说明的：

**P10 为什么不能靠"两个方法各读一次"。** 实测（`tests/test_p10_port.py::test_the_two_call_shape_really_goes_stale`，可复跑；原为一次临时探针）：两次调用各自开一个读事务 ——

```
BEGIN → SELECT turn ... → COMMIT          ← read_turns(1,3)
BEGIN → SELECT content_hash ... → COMMIT  ← read_turn_hashes(1,3)
BEGIN × 2，COMMIT × 2 → 两个独立的读事务
```

中间插一次外部提交，第二次读到的指纹就属于另一个版本：`快照 A 的正文` 与 `快照 B 的指纹` 对不上，而**两边都是"正确读到的数据"**。产出的是"正文与指纹错版"，这个结论既可能是真 P9，也可能是纯粹的读时序——**两者在证据上无法区分**。F16 不可逆，误判一次进程就废了，所以这种"分不清"本身就是不可接受的（`ARCHITECTURE.md` §10.6：不可逆动作要求更硬的证据）。

**但端口形状堵死了这件事**：`read_turns(lo, hi)` 与 `read_turn_hashes(lo, hi)` 都不接 `StoreTxn`、也不接对方的结果，端口上也没有同时交出正文与指纹的组合方法。所以 P10 在**当前签名下无法满足**——不是实现偷懒，是接口没有提供"同一快照"这个入口，调用方只能靠"记得连着调两次"，那是纪律，不是结构。

两条可选修法，我倾向前者，理由与"结构保证分三级"同理（`ARCHITECTURE.md` §2 拿走输入表）：

1. **合成一个方法**：`read_turns_with_hashes(lo, hi) -> (tuple[Turn, ...], Mapping[(int,int), str])`，一次事务取完。这样"跨快照"不是"要小心不要做"，而是**没有可做的入口**。代价是端口多一个方法、少两个。
2. **两个方法都接受同一个 `StoreTxn`**：保留现有形状，把"同一事务"变成调用方的责任——**这正是 P10 想消除的那种责任**，只是挪了个位置。

签名归 `ARCHITECTURE.md` §4.5 定，我不改；这里只把"当前形状满足不了 P10"记成事实。**在签名补齐之前，F16 检出② 不应接线上**——接上去就是把一个不可逆动作建在跨快照证据上。

**P3 为什么不能靠约束。** 「无洞」是跨行聚合条件（`MAX(turn_no) = COUNT(DISTINCT turn_no)`），SQLite 的 `CHECK` 只看单行、`UNIQUE` 只看重复，表达不了。硬要不留缝，只能引入一张「每一轮每个 part 都得存在」的辅助表——那等于把同一事实存两遍，且每轮写入都要维护它。**取舍：宁可在读的时候炸，也不制造第二份真相。** 代价是坏数据能写进去、要到装配时才报 `StoreCorruptError`；补偿手段是 **4.5 的两处常开断言**（`read_turns` 区间校验 + `snapshot()` 全库聚合）加自检 SQL ⑦。**不做成「可选自检」——可选检查等于不跑。**

**P7 已改强承诺（team-lead 拍板）。** 原稿写「单写者假设，只保证不丢文件」，在真实用法下等于**默认丢更新**，而这里的丢更新是**静默**的：payload 照常生成，只是冻结块里少了几个节点，一个报错都没有——比丢文件更坏。改后口径与 schema 已有的能力一致，且这些能力本来就是免费的：`BEGIN IMMEDIATE` 要用来解决升级死锁，partial unique 索引要用来做幂等归并。**弱承诺的真正代价不是少做功能，而是让实现者以为「随便写写就行」**，于是连 6.2 那种必需的连接回收都不会去做。
**P7 的边界一句话：并发写安全 = 不同内容不丢更新 / 相同内容归并为一行 / 不做跨进程语义合并；它只管「不同 session 并发写同一个库」，不覆盖「同一 `session_id` 被多进程驱动」——那条由 P9 禁止。** 别把 P7 读成「同会话也能多驱动」。

**P9 是 P7 的边界，两者不能互相推导。** P7 是**存储层**属性（库不丢数据），P9 是**会话层**属性（同一 `session_id` 只能单进程驱动）。`Session` 持有内存轮次缓冲与 plan 缓存，两个进程各驱同一 session 会各持一份缓冲，同一会话产出不同 payload——这不是数据库能拦的，只能在契约上禁止。team-lead 要的「多进程并发写安全」= 库不丢数据；bench 两臂是两个 **session** 并发写同一个库，正好落在 P7 允许、P9 允许的区间里。

**P8 已改变初稿的一处设计，值得单独记一笔。** 我原先把「同一边界只能压一次」当成幂等保证写成了 `UNIQUE(session_id, turn_no)`。P8 要求 `generation` 追加不覆盖，意味着**允许重压**。这两条不能同时成立。我选了 P8：重压是真实需求（冻结块损坏后按 `PLAN_INVALID` 重建），而「重试提交」这个幂等需求用 `UNIQUE(session_id, generation)` 就够了——重试会拿同一个 generation，重压必然是新 generation。约束边界选在哪，取决于「什么算同一个操作」，而不取决于哪个写起来更省事。

---

## 四、内容指纹与 `rev`

### 4.1 契约
- `rev` **严格等于「真正发生过的内容修改次数」**。重复增量、no-op 归并、空 prune **都不得**递增。
- 判定不看「这次操作改了几行」，只看 `body_hash` 前后是否相等。**`rev` 是 `body_hash` 的函数，不是操作计数的函数。** 这一条把「改标记算不算改」「退役算不算改」这类争议全部消掉：只要它进了指纹，就算。
- **I16：压缩 / 边界不改变 `rev`**（4.6）。写 `plans`、写 `event`、重建 `frozen_text` 都不碰键树，`commit_plan` 里禁止 `UPDATE store SET rev = rev + 1`。

### 4.2 指纹前像（规范形态）
`body_hash = sha256(前像)`，存 64 位小写十六进制不带前缀。

前像按下列顺序拼，记录用 `\x1e` 分隔、字段用 `\x1f` 分隔，末尾补 `\x1e`：

```
"ctx1"
"top="   + (top_text 或 "")
"scope=" + (scope 或 "")
每个 live topic   （ORDER BY num）           : "T" \x1f num \x1f name \x1f (tag 或 "")
每个 live purpose （ORDER BY topic.num, num）: "P" \x1f topic_num \x1f num \x1f name \x1f (tag 或 "")
每个 live leaf    （ORDER BY leaf.id）       : "R" \x1f text \x1f (tag 或 "")
                                              "E" \x1f text \x1f src \x1f nbytes \x1f sha16 \x1f (tag 或 "")
```

**排序口径与 P5 一致**：T 按 `num`、P 按 `topic.num` 再按 `num`、叶子按插入序。这条必须与 `render()` **用同一个序**——两处一旦不一致，`body_hash` 就会与实际渲染字节脱钩，而它们各自看起来都「很确定」，只是彼此矛盾。等价的依据是：编号只增不复用不重排，所以 `num` 序 ≡ 插入序；但等价是**推导结论**，不是契约，两边都写死才安全。

与 `_body_key()` 的差异，三处都是**故意改的**：
1. **分隔符从 `|` 改成 `\x1f`/`\x1e`**。原实现用 `|` 拼字段，而标记允许自由文本（只禁方括号与控制字符），所以 `tag = "a|b"` 与「文本 `a`、标记 `b`」会拼出同一串——**指纹碰撞**，后果是该递增 rev 时不递增，no-op 判定从根上失效。控制字符在名称/标记/摘要/`src` 里全被 V11/V18 禁止（`CTRL_RE`），所以 `\x1f` 作为分隔符不可能出现在字段值里。
2. **无标记记为 `""`，而不是 Python 的 `None`**。原实现 `"%s" % None` 得到字符串 `"None"`，于是「无标记」与「标记字面为 `None`」同串。`CHECK (length(tag) BETWEEN 1 AND 12)` 保证标记非空，`""` 因此不可能与任何真实标记混淆。
3. **`scope` 参与指纹**（沿用原设计）：作用域变了就是内容真的变了。

**与 `snapshot_id` 的关系：没有关系，而且必须没有关系。** `snapshot_id` 由 `(schema, system_sha16, frozen_sha16)` 派生（4.4），与本前像的输入**完全不重叠**。这是刻意的：`body_hash` 只认键树，`snapshot_id` 只认 S+F，两把哈希各自守一侧、没有共享字段，压缩就物理上带不动 `rev`（I16，见 4.6）。反过来说，若让它们共用一份前像（初稿的写法），压缩就成了 `rev` 的输入，某一侧出分歧时也没有第三方能裁决——两把哈希同源，错就一起错。

**为什么 `frozen_text` 不进这个前像**：`rev` 的定义是「键树内容改过几次」。若把 `frozen_text` 拼进来，一次压缩（键树一字未改）就会让 `rev` 凭空 +1，no-op 判定与审计口径同时失真。后果不是偏移一格，而是**此后每一次 no-op 判定都错**：拿旧指纹比新内容，永远「有改动」。冻结块自有一把锁：`plans.frozen_sha16`。

### 4.3 确定性渲染
`render()` 的排序只允许三样：T 按 `num`、P 按 `topic.num`+`num`、叶子按 `id`（插入序）。不许出现：按 name 排序、按 tag 排序、依赖 `GROUP BY` 的输出顺序、依赖索引扫描顺序、依赖 `id` 与 `num` 恰好一致这个巧合。SQL 里必须**显式**写 `ORDER BY`——`id` 是 rowid、全表扫天然有序，这是**实现细节**，不能当契约用。

### 4.4 `snapshot_id` 的定义（P6 的落地规范）

```python
# 形态说明，不是实现代码
pre = "\x1f".join(["ctxsnap1", str(schema), system_sha16, frozen_sha16])
snapshot_id = hashlib.sha256(pre.encode("utf-8")).hexdigest()[:16]
```

**它唯一的职责是回答一个问题：「决定 S+F 字节的那份内容，还是不是同一份」。** 既然只回答这个，输入就只有两个权威哈希，别的全部不进。

这条定义我改过三次，过程值得留档，因为它就是 A7 的现场：

1. **初稿 `snapshot_id ≡ body_hash[:16]`**（"不引入第二套哈希"）。被 P8 打掉：`body_hash` 只覆盖键树，F 换了它一字不变，于是"F 换过"这件事 `snapshot_id` 根本看不见。
2. **第二稿：为 S+F 另算一份 canonical dump**（五条记录 + 分隔符 + 编码 + 前导零规则）。被否掉的理由是「派生量优于再定义」：dump 的每一条格式规则都是未来的争议点，而新方案的输入是两个 16 位定长小写十六进制 + 一个整数 + 一个常量前缀，**不存在分隔符歧义**。
3. **定稿：从两个权威哈希派生。** 结构上不可能不一致——派生量不会与它的源打架，这比"小心维护两份 dump 让它们一致"高一个量级。

**排除清单不需要维护，因为没有任何其它输入**：键树、`rev`、rowid、时间戳、路径、`turn` 边界、历史 `generation`。

**`generation` 为什么不进**：它是计数器，不是内容。重压后若 F 字节恰好相同，那它就是**同一份冻结状态**，`generation` 不同不改变这个事实。真要一个"每次边界都唯一"的串，`(session_id, generation)` 本身就够了。

**`policy_digest` 为什么不进**：policy 影响的是"**下次**压缩怎么选"；已经冻结的 F 字节由 `frozen_sha16` 完整表达。多一个输入只是多一处可以不一致的地方。（这个版本的公式被 team-lead 与顾图南各自独立否掉，理由一致。）

**为什么键树必须排除——后果链**：

> 键树进 `snapshot_id` → 每 `add` 一条要求 id 就变 → 每轮判 `PLAN_INVALID` → F 每轮重建 → 压缩彻底失效 → **不报错、不崩溃，只是成本静默翻几倍**。

反方向是同一条纪律：`body_hash` 若含 `frozen_text` → 一次压缩凭空 `rev + 1` → `rev` 从"键树改过几次"变成"做过几次操作" → P4 破裂 → **此后每次 no-op 判定都错**。这就是 I16（4.6）。

**"F 不新鲜"不是问题——F 本来就应该不新鲜。** I3 说的是"F 只在边界重写"，不是"F 永远最新"。两次边界之间键树在长、F 是定格的，这正是冻结的全部意义：让前缀稳定。新鲜度由下一个边界（N=12 轮或预算压力）负责，不由哈希负责。

> **A7：`plan.store_rev` 与 `plan.snapshot_id` 是审计字段，不是有效性闸门。**

"与当前内容不一致"是设计意图，不是故障——因为 F 的职责就是冻结在过去。把这句话反过来用（不一致即无效），正确性断言会全绿，只有成本静默恶化，是这套设计里最难发现的一类错。

三个哈希的分工定死：

| 哈希 | 覆盖 | 用途 | 参与控制流？ |
|---|---|---|---|
| `body_hash`（64 hex） | top / scope / 键树正文（T/P/叶） | 驱动 `rev`（P4）、no-op 判定 | **是**（唯一一个） |
| `snapshot_id`（16 hex） | `schema` + `system_sha16` + `frozen_sha16` | 审计：回答"这份 F 配的哪份 S、用的哪版 schema" | 否 |
| `plans.frozen_sha16`（16 hex） | `frozen_text` 自身 | 读时校验，不符即 `StoreCorruptError`（P8 / `PV1`） | 是（仅自洽） |

注意 `body_hash` 与 `snapshot_id` **没有共享输入**。这不是巧合，是 I16 的实现方式：两把哈希的覆盖范围互补且不重叠，压缩与 `rev` 就物理上解不开耦合。

**P6 的落地依赖：`frozen_sha16` 必须取自落库值（P8 + I6），不能在装配期重算。** `snapshot_id` 的输入里那个 `frozen_sha16` 不是算出来的，是**从 `plans` 行里读出来的**。一旦哪天 F 改成装配期重算，两次运行就可能得到不同的 `frozen_sha16`，`snapshot_id` 随之不同——**P6 的"可复现"就这样在没人动 P6 一个字的情况下失效**。

所以这条依赖必须写死：**P6 的可复现性不由 P6 自己保证，由 P8（F 落库为列）+ I6（边界之间绝不重算）保证。** P6 那一行单看是自足的（"由两个权威哈希派生"），它读到的是不是权威值，要看另外两条。这是"契约表按单条读会漏掉耦合"的第二个实例（第一个是 P3 的挂载层级依赖 P7）。

**"S 变了要重压"的判定由 `PV2` 提供，不绕道 `snapshot_id`。** 注意分工：**规则住在 `I2`**（S 在整个会话内不变），`PV2` 只负责**发现**它变了，发现之后才驱动重压。`plan.system_sha16` 与当前 session 的 system sha 直接比，一个字段相等的事，而且它是**必须发生**的判定。一旦改写成"`snapshot_id` 与当前状态是否相等"，就又回到 A7 那个坑：把一个散列比较当成闸门，闸门的输入还包含了不该包含的东西。

**"成员约束"这条现在不需要维护了**：初稿要写一长串"不许含 `rev`、不含 rowid、不含时间戳……"，而定稿里这些量**没有任何路径进来**——排除是靠构造做到的，不是靠清单做到的。清单会漏，构造不会。

**三条实现禁令**（顾图南明确要求，写在这里防止实现时又把雷埋回去）：

1. **`plans.store_rev` 只写不判。** 它是审计字段。
2. **不要提供 `is_plan_stale()` / `plan_matches_snapshot()` 这类便利方法。** 名字里带「是否有效」的 API，早晚会有人拿它当闸门用——而这正是刚修掉的那个 bug。
3. **`snapshot_id` 不做「与当前状态比对后报错」的校验。** 唯一参与校验的是 `frozen_sha16`（对 `frozen_text` 自洽）。

这三条的**机械检查形态**（可 grep、建议做成常开）见 **ARCHITECTURE.md §2.2**——那里把「审计字段只许写入与原样输出」写成了四行表格：清单 / 违规形态 / 允许形态 / 豁免登记。**语义规则写在这里，扫描规则写在那里，不要在本文再抄一份清单**：抄了必然漂移，而漂移的表现是「检查还在跑，只是少查了一个字段」，不会有任何报错。

**唯一保留的比对**：`snapshot()` 返回的 `snapshot_id` 由**当前 S + 最大 generation 的 F** 现算，与 `plans.snapshot_id` **并列输出、不做相等断言、不触发任何降级**。两者不等只有两种来源：S 换了（`PV2` 检出 → 规则在 `I2`）、`schema` 升了级（`plans.schema` 与当前 schema 直接判）——两件事都已有直接判定，不必也不该让一个散列去重复表达。

### 4.5 逐轮断言（P3 的常开检查，不是可选自检）

P3「轮次连续无洞」不能靠约束（`CHECK` 看单行、`UNIQUE` 看重复，无洞是跨行聚合条件）。但也**不许做成"可选跑一次"——可选检查等于不跑**。两处常开：

| 位置 | 挂在哪一层 | 断言 | 触发后走哪条 | 成本 |
|---|---|---|---|---|
| `read_turns(lo, hi)` | **读路径（`send()` 内）** | 返回条数 `== hi - lo + 1`，且区间内 `turn_no` 逐值出现 | **F1**：静默降级 `buffer_only`，记 `STORE_CORRUPT` | O(区间)，唯一索引直取 |
| `snapshot()` | **装配前取数** | 同一读事务内 `MIN(turn_no)==1 AND MAX(turn_no)==COUNT(DISTINCT turn_no)` | **F1**：同上 | 一次聚合，唯一索引覆盖 |

第一条是主力（只校验真正要用的那段）；第二条兜「整库被外部删改」。两者都在**同一个读事务**内，否则断言本身会被并发写打出假阳性。

**为什么"炸在装配时"可以接受**：`read_turns` 抛 `StoreCorruptError` → 上层走 **F1** → 降级 `buffer_only` payload。它是**被 fail-open 接住的降级**，不是用户可见的崩溃。这是不加辅助表的正当理由——加辅助表会制造第二份真相，且辅助表自己的洞与正文的洞无法区分。

**"挂在哪一层"不是实现细节，是契约决定**（顾图南补）。同一条 P3 违规挂在不同层，映射到**两条不同的失败矩阵条目**，对调用方的语义完全不同：

| 挂载点 | 抛什么 | 失败矩阵 | 调用方看到 |
|---|---|---|---|
| `read_turns()` / `snapshot()`（本设计） | `StoreCorruptError` | **F1** | 系统**已经替你降级**了，你只会在报告里看到一条 `STORE_CORRUPT` |
| `open()`（本设计**不用**） | `StoreError` | **F2** | **构造期大声失败**，你必须在那一刻显式决定是否改用 `BaselineArm` |

所以「为了早点发现，把它挪到 `open()`」**不是重构，是改契约**：测试里期望抛异常的地方会变成静默降级，期望降级的地方会变成构造就炸。两边都不报错地错位。

若确实想在 `open()` 加一次 P3 全表扫描，那是**新增一个 F2 检查点**，必须显式声明为 F2，并且 `read_turns` 的常开断言**不能因此删掉**。理由不是"多点保险"，而是：

> **检测点的有效期 = 它之后第一次外部写。**（顾图南的表述，比"打开那一刻"准确）

有效期不由时间决定，由**写**决定。而"允许外部写"这件事，依赖的是 **P7**（不同 session 可并发写同一个库）——所以这条推论是把 **P3 与 P7 接起来看**才成立的，**单看 P3 推不出来**。这大概也解释了为什么两边都写了 P3、却都漏了挂载点：P3 是单点契约，它的落地方式取决于另一个契约的强弱。

**检测点位置决定降级语义，这是"两处常开"必须写清各自层级的原因。**

### 4.6 I16：压缩不改变 `rev`

> **I16｜压缩 / 边界不改变 `rev`。** 写 `plans`、写 `event`、重建 `frozen_text`，都不碰键树内容，因此**不得** `UPDATE store SET rev = rev + 1`。任何把 `rev` 与"发生过多少次压缩"挂钩的实现都是错的。

单列一条，是因为 `commit_plan`（T3）里顺手加一句 `rev + 1` 是最自然的写法，而且**读起来完全说得通**（"我写了一条新记录"）。但 `rev` 的定义是"键树内容改过几次"，一旦掺入操作次数，此后每一次 no-op 判定都拿错基准——P4 从根上失效，且失效方向两边都静默：要么该递增时不递增（改动不记），要么压一次缩就让下一轮"检测到改动"（无改动也递增）。

三条落地约束：

1. **`commit_plan` 的写集 = `plans` + `event` + `system_prompt`。** 前两张是主体，第三张是幂等附赠：`system_prompt` 按 `UNIQUE(session_id, sha16)` 内容寻址 + `DO NOTHING`，重写同一份 system 不改任何一行，既不属于键树也不动 `rev`。**唯一被禁止的是 `UPDATE store`** —— 连 `updated_at` 也不碰（压缩不改变键树正文，`updated_at` 属于键树）。（初稿此处写的是「只含 `plans` + `event`」，与实现不符：实现在同一事务里先 `put_system`。口径按实现订正——否则下一次有人照文档审写集，会把一条合法语句判成越界。）
2. **`body_hash` 与 `snapshot_id` 的输入集不相交**（4.4）：前者只认键树，后者只认 S+F。**这不是约定，是结构**——"压缩改了 F" 在物理上没有路径能传到 `rev`。纪律会被忘，路径不存在不会。
3. **自检可查**：同一 `plans.generation` 前后，`store.rev` 必须相等。测试用例：连续 `commit_plan` 三次（键树一字未改），断言 `store.rev` 三次都不变，且 `body_hash` 三次都不变。

第 2 条要防的是一个听上去很合理的优化：**"让 `body_hash` 也覆盖 plan，这样一次哈希就够了。"** 这句话一旦落地，就把刚刚拆开的两侧重新接上——`snapshot_id` 那次已经证明后果（每轮 `PLAN_INVALID`、每轮全价轮、不报错）。所以"两个哈希要不要合并"的答案是**永远不合并**：它们不是同一件事的两种算法，是两件不同的事各自一把锁。

同一个手法在这套设计里用了三次，都值得当成范式：

| 场合 | 把什么从"要记得"变成"做不到" |
|---|---|
| 确定性渲染 | 装配器不 `import` store/clock，想读也没得读（A3） |
| F 落库 | F 的文本存在 `plans` 里，没有"重算 F"的 API 可调（I6） |
| **I16 本条** | 两个哈希输入不相交，`rev` 没有路径被压缩影响 |

对称地，`rev` 也不该驱动压缩：**"rev 变了就重压"是同一个错误的镜像**——`rev` 在边界之间天天变，那等于每轮都重压。压缩只由 `Policy.boundary_due`（ARCHITECTURE.md §9.1）的两个条件触发：N 轮或预算压力。I3 在这里的作用是**约束重写地点**（F 只能在边界重写），不是提供触发条件——别把不变量当触发器用。

---

## 五、事务边界

**必须在一个事务里的操作组合，以及为什么：**

| # | 事务 | 包含的语句 | 为什么不能拆 |
|---|---|---|---|
| T1 | 追加一轮 | `UPDATE session SET turn_seq = turn_seq + 1` 取号 → `INSERT turn`（1..n 条 part）→ `INSERT measure`（含 `ok=0`） | turn 落库而 measure 丢失 → bench 的分母（轮数）与分子（token）来自两次提交，比例失真且事后无法修复；反过来有 measure 无 turn → 回放测试复现不出当时的输入。号在其中被取用，也必须与插入同事务，否则崩溃留下「号已发、行不存在」的永久空洞 |
| T2 | 并入键树（`add`） | 分配 T/P 号（`num_alloc`）→ upsert `topic`/`purpose`（命中则可能改标记）→ upsert `leaf`（命中则改标记或 no-op）→ 重算 `body_hash` → **相等则 `ROLLBACK`**，不等则 `UPDATE store SET rev=rev+1, body_hash=?, updated_at=?` | 编号、内容、rev 三者必须同时可见。拆开会出现「号已分配但行不存在」（按不复用原则即永久空洞）或「rev 落后于内容」（指纹失效，之后所有 no-op 判定连锁出错）。`ROLLBACK` 分支是把「无变化不写盘」做成默认：no-op 连 WAL 都不写，且分配器一并回滚，不泄漏编号 |
| T3 | 提交压缩（`commit_plan`） | `INSERT system_prompt`（内容寻址，`DO NOTHING`）+ `INSERT plans`（新 generation）+ `INSERT event`（kind=compaction）；重压时**不删旧 plan**；**不 `UPDATE store`**（I16：`rev` / `updated_at` 都不碰） | `frozen_text` 与 `epoch_turn` 必须同时可见：分两次写，中间崩溃会得到「边界存在但冻结块为空」——该区间的内容既不在窗口里，也不在冻结块里，**凭空消失**，是本设计里唯一会造成不可恢复丢失的形态。`event` 同事务是为了「plan 存在但报告里没有这次压缩」不会发生：报告会漏计边界成本，而 MEASUREMENT-SPEC.md §3 明确要求边界轮的缓存失效必须计入。写 `store.rev` 是最自然的错误（"我写了一条新记录"），见 4.6 |
| T4 | 降级落账 | `INSERT event`（kind=degradation，`at_turn` + `at_tick` + `fallback`） | 再小也要事务：它是「这一轮不再可信」的唯一证据，静默丢一条等于把一次降级从报告里抹掉 |
| T5 | 建库 | `INSERT store` + `INSERT num_alloc` 种子（T:0 / 每个 P 桶） | 种子缺失时第一轮分配会撞 `UNIQUE(store_id,num)`，而不是从 1 开始 |
| T6 | 删除整库 | `DELETE FROM store WHERE id=?` | 下游全靠 `ON DELETE CASCADE`。这是唯一的物理删除入口 |

**读路径也是事务**（最容易漏的一条）：`render()` / `assemble()` 要读 `store` + `topic` + `purpose` + `leaf`，多语句。WAL 下每条语句各自取快照，并发写会让三条 SELECT 分别落在不同提交上——拼出「T 是旧的、R 是新的」的**混合快照**：渲染字节不可复现，`rev` 与实际内容对不上，而且**结果仍然是个看起来完全正常的字符串**。所以读必须包一个 `BEGIN`（DEFERRED）+ 多 SELECT + `COMMIT`。

**绝不在事务内做的事**：调用模型。写事务只包本地写；provider 往返（秒级到分钟级）在事务外。理由见第九节 R4。

---

## 六、连接级 PRAGMA 与并发模型

### 6.1 必备 PRAGMA

```sql
PRAGMA journal_mode = WAL;      -- 持久化到库文件，只需成功设一次；重复执行无害
PRAGMA synchronous  = NORMAL;   -- WAL 下：断电可能丢最后一个已提交事务，但库不损坏
PRAGMA foreign_keys = ON;       -- 每连接生效，SQLite 默认 OFF
PRAGMA busy_timeout = 5000;     -- 毫秒。协程/线程池里请按「最长写事务」调
```

四个必须知道的坑：
1. `foreign_keys` **每个连接都要开**，默认是关的。不开时 `REFERENCES` 只是一句注释，级联删除与引用完整性全部失效，且不报错。
2. `foreign_keys` 与 `journal_mode` **不能在事务内设置**，会被静默忽略。必须在连接初始化时、`BEGIN` 之前执行。
3. `busy_timeout` 只解决「等多久」，不解决「升级死锁」。写事务一律 `BEGIN IMMEDIATE`：立刻拿写锁，拿不到就等/退避。用默认的 DEFERRED 时会「先读后写」，两进程各持读锁再同时想升级 → `SQLITE_BUSY` 且不可重试，只能整事务重来。
4. `busy_timeout` **只管 `SQLITE_BUSY`，不管 `SQLITE_READONLY`**。只读错误是另一类故障，处理方式完全不同（丢弃连接，不是等待）——见 6.2。

并发模型：**单写多读**。
- 写：`BEGIN IMMEDIATE` → 语句 → `COMMIT`。WAL 下写不阻塞读，读不阻塞写（只与写互斥）。
- 读：`BEGIN`（DEFERRED）→ 多 SELECT → `COMMIT`。单语句读可不显式开事务。
- `SQLITE_BUSY` 重试的是**整个事务**（含 `body_hash` 重算），不是最后一条语句。

`rev` 递增靠写事务串行化 + `body_hash` 比较，不靠乐观锁版本号：库被序列化了就不需要 CAS，多一层版本号只会多一处会不一致的状态。

### 6.2 连接生命周期：`readonly` 是粘在连接上的（已实测）

这一条不是理论推导，是压测里踩出来的，**必须在实现里写死**。

在 WAL 库上，若多个进程**同时冷启动**打开同一个库（上一个连接刚关、`-wal`/`-shm` 已被删除，几个进程一起去创建 `-shm`），抢输的那个进程会被 pager **永久标记为只读**。症状：

```
sqlite3.OperationalError: attempt to write a readonly database
```

实测（8 进程 × 25 事务，反复冷启动，Windows / Python 3.13 / SQLite 3.53）：

| 做法 | 结果 |
|---|---|
| 同一连接上重试 | **0 / 25 成功**——该连接此后每个事务都失败，一次都不例外 |
| `busy_timeout=10000` | **完全无效**：这不是 `SQLITE_BUSY`，等多久都不会变可写 |
| 捕获 `readonly` → `close()` 后重开 | **全部成功**，每轮自愈 1–5 次 |
| 进程内保持一条常驻连接（不频繁开闭） | 8/8 进程零失败 |

三条结论，直接决定实现形态：

1. **`readonly` 标记该连接待回收，绝不重试语句或事务**，也不要指望 `busy_timeout`。但**回收动作发生在下一次写入之前，不在本次调用内**（热路径不重试，见 6.3）。重开几乎总能立刻成功。
2. **库的生命周期内保持至少一条常驻连接**（连接池 / 单例），让 `-shm` 不被反复删除重建。这是从源头消除竞争，比事后自愈更好。
3. 连接初始化时做一次**可写性探针**：`BEGIN IMMEDIATE` + 一次 no-op 写（如 `PRAGMA user_version = user_version`）+ `ROLLBACK`。探针失败即换连接，免得把一条只读连接交给业务代码——它会在第一次 `add` 时才炸，而那时已经离故障点很远了。

相关地：库文件**不要放在网络盘或同步盘**（OneDrive / Dropbox / SMB）上。`-shm` 依赖 mmap 与文件锁语义，同步盘会把偶发的 `readonly` 变成常态，且是那种「单机跑得好好的、换台机器就丢数据」的形态。这一点要在 SDK 启动时显式检查并告警。

### 6.3 错误映射与「不在热路径重试」（与 6.2 的张力如何处理）

顾图南定的是：`commit()` 失败抛 `StoreIOError`，上层转 `readonly_mode`，**存储层不要做重试循环**（会拖住热路径，且 `put_turn` 必须走 `send()` 的旁路）。

这条与 6.2 的「粘性只读必须换连接」看似矛盾，其实不矛盾——**关键是换连接的时机，不是次数**：

| 时机 | 做法 | 是否拖热路径 |
|---|---|---|
| 本次 `commit()` 失败 | **不重试，立刻抛出** `StoreIOError`。上层按 F3 转 `readonly_mode`；payload 已生成，写盘是旁路 | 否 |
| 下一次**新的**写事务开始前 | 若该连接上次抛过 `readonly`，先 `close()` 再重开，然后正常执行 | 否（只发生在已故障的连接上） |
| `open()` 时 | no-op 写探针（`BEGIN IMMEDIATE` + `PRAGMA user_version = user_version` + `ROLLBACK`），失败即换连接，最多 N 次 | 否（启动期，不在 `send()` 上） |

**为什么不能完全不换连接**：只读是**粘在连接上**的（6.2 实测：同一连接 25/25 全败）。若只抛错不换连接，该进程此后**每个新会话**的第一次写都会失败——上层看到的是「磁盘坏了」，而实际上换条连接就好。故障被永久放大成「这个进程写不了盘」。
**为什么不能在 `commit()` 里换连接再试**：那等于在一次 `send()` 里做「失败→重连→重放事务」，重放可能重复副作用，且把秒级故障变成热路径长尾。所以语义定为：**本次失败就是失败（如实上报），修复留给下一次调用**。

错误码映射（`store_sqlite.py` 的唯一职责，别处不要再判 errno）：

| SQLite 错误 | 映射 | 上层行为 |
|---|---|---|
| `SQLITE_FULL` | `StoreIOError`（errno=ENOSPC） | F3：`readonly_mode`，`DISK_FULL` |
| `SQLITE_READONLY` | `StoreIOError`（errno=EACCES）+ **标记该连接待回收** | F3：`readonly_mode`，`STORE_IO` |
| `SQLITE_BUSY` / `LOCKED` | **不映射**：`busy_timeout` 内由 SQLite 自己等；超时后才 `StoreIOError` | 超时 → F3 |
| `SQLITE_CORRUPT` / `NOTADB` | `StoreCorruptError` | F1：跳过存储，用内存缓冲装配 |
| `UNIQUE` / `CHECK` / `FOREIGN KEY` 违反（**默认**） | `StoreCorruptError`（代码 bug 或坏数据，**不是 IO 故障**） | F1 |
| **例外：`put_turn` 发现同一 `(turn_no, part)` 内容不同** | **`SessionConflictError`** | **F16**：记 `CONCURRENT_DRIVER`，本进程此后**彻底停写**（写事务内证据 → 单次即够，见 `ARCHITECTURE.md` §10.6） |
| 其余 | `StoreError` | 未分类 |

**那条"例外"不能靠读错误文本判断**（顾图南的要求：**类型归分派，文本归日志**）。分派依据不是消息里有没有 `plans.session_id, plans.generation` 字样——文本随 SQLite 版本与消息格式漂，分派不该跟着漂——而是**调用点**：

> 整个存储层只有 `put_turn()` 会在同一个 `(turn_no, part)` 上做内容比较，所以**只有它**把「同号不同内容」译成 `SessionConflictError`；其他所有写路径的 `IntegrityError` 一律 `StoreCorruptError`（或按可达性指向实现的 `InvariantError`）。
>
> **同一条 `IntegrityError`，含义由「当时在做什么」决定，不由消息决定。**

但「调用点」只够划到**方法**，划不到**语句**。`put_plan` 的那条 `INSERT` 除了 `UNIQUE(session_id, generation)`，还会撞四条 `CHECK`（`r_first_turn <= epoch_turn + 1`、`generation >= 1`、`frozen_text` 非空、`store_rev >= 0`）和外键——这些全是「压缩算错了」，是 F1 的活。所以作用域必须再窄一格，而且两个 `plans` 情形的译法**相反**：

> **`put_plan` 一侧没有 `SessionConflictError`。**
> * **`CHECK` / `FOREIGN KEY` 违反** → `StoreCorruptError`（F1）：内容不合法。
> * **`UNIQUE(session_id, generation)`** → **`InvariantError`**：号在写事务内分配（P11），两个驱动永远错开，**撞不到**；真撞到只可能是"号被挪到事务外算"，那是实现缺陷 —— 译法必须**指向写代码的人**。
> * **pre-check（同号 + 比冻结字节）不再是错误分支**：同号同字节 = 重试去重（返回原号、不新增行）；同号不同字节 = 拿着过期 snapshot 在写（**发一个新号**）。两者都**不抛**。

而**唯一**译成 `SessionConflictError` 的是另一处：**`put_turn` 发现同一 `(turn_no, part)` 内容不同**。它有真阳性（另一个驱动写同一轮，P9 被违反），且发生在**写事务内**、不跨快照——按 `ARCHITECTURE.md` §10.6，这类证据**单次即够**，不需要二次确认。

> 两处形状几乎一样（都是"同一个号、不同内容"），结论相反，原因是**可达输入集相反**：
> `plans` 的唯一键在正确实现下**没有任何输入能命中**（→ 指向实现）；`turn` 的分叉有真实输入（→ 指向调用方）。
> 判据全文见 `ARCHITECTURE.md` §10.4「同一条判据，两个相反结论」。

把整个方法的 `IntegrityError` 一口吞成 F16 的代价是实测过的：让压缩少算一位 `r_first_turn`（越过 `epoch_turn + 1`），会话被诊断成 `concurrent_driver`、`store_writable` 转 `False`、此后每一轮都带着这个原因发出去——**进程模型没错，却被永久停写**，而排查方向被引到一个根本不存在的「第二个进程」上。

还有更硬的一层：`BEGIN IMMEDIATE` 从进入 `put_plan` 就握着写锁（6.2），而号在**同一个事务内**按 `MAX(generation) + 1` 分配（P11，`put_plan` 返回分配到的号，调用方必须采纳回内存）。所以「两个驱动撞唯一键」不是"撞了再翻译"的问题，而是**撞不上**——后来的那个看到的 `MAX` 已经含前一个。**用结构消掉一条分支，比给它配一个正确的译法更强。**

一句话概括这两层：**语义在最知情的层产生，分派在最无知的层消费。** 两边各错开一格都会坏，而且都坏得很安静——存储层若改按约束名判，措辞一换就换降级路径；降级层若改看文本或栈，它就开始在分支里重新解释业务。

这也顺带定位了 R10 那条"看到唯一键冲突先怀疑 P9"的诊断规则：它是**给人看的排障提示，不进任何 `if`**。类型负责分派，文本负责排障——两者都需要，但混用会导致"换个 SQLite 版本行为就变"。

最后一行要强调，而且理由比"分类不准确"严重得多：按失败矩阵，`StoreIOError` 走的是 **F3** → 会话转 `readonly_mode` → **继续静默服务**。于是"写入逻辑有个真 bug"被伪装成"磁盘暂时满了"，payload 照常产出、bench 照常跑完，**永远不浮出水面**。反过来 `StoreCorruptError` 走 **F1** → 跳过存储、用内存缓冲装配 → 断言失败、测试红、问题当场暴露。

一句话：**`StoreIOError` 是"环境坏了，等等会好"；约束违反是"我们写错了，等多久都不会好"。** 前者可以降级续跑，后者必须大声。错误分类错了，降级路径就全错，而且每次重试都必然再失败一遍——只是没人再看得见。

同一推理还解释了为什么 `SQLITE_BUSY` **不映射**：它确实是"环境暂时不好"，但如果映射成 `StoreIOError`，一次正常的锁等待就会把整个会话打成 `readonly_mode`，且**永不恢复**（`readonly_mode` 是会话级粘性状态）。让 SQLite 自己在 `busy_timeout` 内等，是唯一不制造假故障的做法。

---

## 七、迁移策略

版本载体：`PRAGMA user_version`（库内一个整数，随库文件走，不额外建表）。

**打开流程**
1. 读 `PRAGMA user_version` 得 `v`。
2. `v == 0`（新库）→ 执行第二节 DDL → 一个事务内 `PRAGMA user_version = 1` → 建 `num_alloc` 种子。
3. `0 < v < CURRENT` → 依次执行 `v+1 … CURRENT` 每一步。
4. `v > CURRENT` → **拒绝打开**。旧代码读新库会静默忽略不认识的列、按旧语义写回，把新字段写丢；报错比「看起来能跑」安全得多。

**每一步迁移的规矩**
- 一步一个事务：DDL + `PRAGMA user_version = n` 同事务提交。SQLite 的 DDL 可回滚（`VACUUM` 除外），所以失败即整体回滚，版本号不会前进。
- **只做加法**：加表、加列（带 `DEFAULT`，或 `NOT NULL DEFAULT`）、加索引。这三类在旧代码下也安全（旧代码只是不读新列）。
- 改语义（改类型、拆列、改 `CHECK`）走**影子表**：建新表 → `INSERT … SELECT` 复制并转换 → 同事务内 `DROP` 旧表 + `RENAME` 新表。不要指望 `ALTER TABLE` 能改约束，它只会重建表。
- 删列分两步走：先 `v_n` 停写停读，`v_n+1` 才 `DROP`。一次到位就没法回滚。
- 加 `CHECK` 更严的枚举值（如新增 `role`、新增 `arm`）= 语义变更，必须同时改迁移脚本与 MEASUREMENT-SPEC / TRANSCRIPT-FORMAT，否则「库允许、口径不允许」。
- 每步必须**可重入**：用 `user_version` 严格推进，不要靠「先查表是否存在」来判进度（`CREATE TABLE IF NOT EXISTS` 会让人误以为迁移天然幂等，其实只对加表幂等，对加列不）。

**回滚方案（强制，不是建议）**
- 迁移**前**：`VACUUM INTO 'store.db.bak.v{from}'`。它给出一致快照且不中断服务（3.27+）。目标文件已存在时 `VACUUM INTO` 会报错，命名带版本号可避免自撞。
- 迁移后立刻跑：`PRAGMA integrity_check` + `PRAGMA foreign_key_check` + 一次 `render()` 字节比对（与迁移前导出的 `.ctx` 逐字节比）。三项全过才算成功。
- 只有「新版本已跑通一次完整 bench」之后，才允许删除 `v{from}` 备份。删早了，回滚就变成重放——数据可以丢，`rev` 语义无法重建。
- `.ctx` 导入是一次性工具，不是迁移：导入后 `store.fmt_version` 记录来源格式版本，但**不承诺导入后再导出逐字节相同**（见第九节第 8 条）。
- **导入 legacy `.ctx` 时 `rev` 必须重算，不能沿用原文件里的 rev。** 本 schema 的指纹前像换了分隔符（`\x1f`/`\x1e` 取代 `|`，见 4.2 第 1 条修掉的碰撞），哈希体系与旧实现不通用。若把旧 `rev` 直接搬进库，之后**第一次 no-op 判定就会拿旧口径的 rev 与我们的指纹比**——从第一天起每次判定都可能错，而且错的方向是「该递增时不递增」或「不该递增时递增」，两边都静默。正确做法：导入时按新前像算 `body_hash`，`rev` 从 0 起，把旧 rev 记到 `store.fmt_version` 旁边的元数据里只作参考。**一个计数器只有在与它的指纹同源时才有意义。**

---

## 八、风险与对策

> 十一条里只有 **R8 是经多进程压测复现并量化过的**（其余是按格式语义与契约推出来的），建议先看 6.2。
> R1–R9 是数据与并发风险；R10 / R11 是**语义误用**风险——它们不破坏任何一个字节，只让成本或正确性静默偏掉，所以单独放在最后两条。
> **编号消歧**：本节的 `R1…R11` 是**风险**编号。ARCHITECTURE.md §9.1 的 plan 有效性校验已改名 **`PV1…PV4`**（原 `R1–R4`，与我这里撞名且指代相反），本文引用它时一律写 `PVn`，不需要再加限定词。两套编号的分工登记在 **ARCHITECTURE.md 附录 A.0 编号约定**——那是命名空间的唯一登记处，新增任何带编号的清单前先去那里登记。

### R1（最隐蔽）混合快照：多条 SELECT 落在不同提交上
读路径不开事务，`store` / `topic` / `leaf` 三次 SELECT 之间若有并发写：`rev` 来自旧提交、内容来自新提交。渲染出一个**语法完全合法、字节可复现、但两者互相矛盾**的字符串。`validate` 查不出来（它只校验结构与计数），因为错的不是结构，是「哪一版」。
**对策**：读路径包 `BEGIN`（DEFERRED）；`render` 的判定输入（`rev`、计数、内容）必须来自同一快照；测试用「边写边读」双进程压测，断言每次输出都能被 `parse()` 且 `body_hash` 与输出内容自洽。

### R2（最容易写错）改标记被 upsert 静默吞掉，或反过来把 `[钉住]` 清空
`ux_leaf_r` 上了之后，写路径必须走 `ON CONFLICT DO UPDATE`。这里有两个方向相反、都会静默出错的写法：
- 写成 `INSERT OR IGNORE`：**「同文本 + 新标记 = 改标记」变成 no-op**。`[已废]` 从此打不上，`prune --drop-stale` 永远没有可删的条目，审计留痕整条链断掉——而且一个错误都不报。
- 写成 `DO UPDATE SET tag = excluded.tag`：当**本次没带标记**（`tag IS NULL` 表示「不动标记」）时，会把已有的 `[钉住]` **清成 NULL**。后果是用户明确钉住的内容被 `prune --aging` 折叠掉——**静默数据丢失**，比不生效严重得多。
**对策**：`SET tag = COALESCE(excluded.tag, leaf.tag)`。并且明确：`rev` 是否递增不看「有没有走 UPDATE 分支」，只看 `body_hash` 前后是否相等（第四节）——把「改标记算不算改」这个问题彻底从分支逻辑里删掉。
**已知副作用**：`COALESCE` 意味着**标记无法被清回 NULL**。这与原格式一致（`_split_tag` 无标记时返回 `None`，`apply_delta` 只在 `tag is not None` 时写），所以不是退化，是**继承的限制**；要清标记必须走显式 API（将来加 `clear_tag` 列或独立操作），不能靠「不带标记的重复行」。

### R3 编号被回收 → 外部引用静默指错
任何物理删除 + 「最小未用正整数」的组合都会让旧号复活。`T2` 在导出 JSON、索引文件、人的笔记里都出现过；号被回收后，这些引用指向另一个事物，**没有人会收到通知**。
**对策**：三重防线——键层行只 `state='retired'` 不 DELETE；`num_alloc.last_num` 只增；`UNIQUE(store_id,num)` 把「同号两行」变成写入失败而不是数据分叉。
**连带取舍**：因此**不支持原地 `prune --compact` 重排**。需要重排 = 导出为一个新 `store_id`（新库首任编号从头来），旧的留着只读。原格式允许重排是因为它明确要求调用方确认「外部引用会失效」；在 SDK 里没有「调用方确认」这个交互环节，只能不给这个操作。

### R4 长事务跨网络 IO → 全员 busy_timeout，WAL 无限增长
把 provider 调用放进 `BEGIN IMMEDIATE`：写锁被持有几十秒，其它进程全部超时；更隐蔽的是读快照长时间不释放，**WAL 无法 checkpoint**，`-wal` 文件涨到 GB 级，最后所有人都变慢。
**对策**：事务边界只包本地写（T1 里的「取号 + 插 turn + 插 measure」在 provider 返回**之后**执行，取号也在那时才做）；禁止在事务内 `await` 网络；测试里注入一个慢 provider，断言写锁持有时间只与本地写有关。

### R5 测量虚增与幸存者偏差
重试/超时重放让同一轮出现多条测点，均值权重被放大；失败样本若不入库，被剔掉的恰恰是 payload 最大、最容易截断的轮次——结论会偏好方案。
**对策**：`UNIQUE(session_id, turn_no, attempt)`；聚合时对每个 `(session, turn, arm)` 取 `MAX(attempt)`（或显式按 attempt 过滤），这一点写进 report 口径；失败一律落一行 `ok=0`，并在报告里报「失败轮数 / 总轮数」。`cached_tokens <= input_tokens` 的 `CHECK` 兜住最常见的一类双计。

### R6 `body_hash` 与 `rev` 分叉
在事务外算 hash 再写：崩溃后 rev 与内容不一致。此后**每一次** no-op 判定都会错（拿旧指纹比新内容 → 永远「有改动」→ rev 空转；或反过来 → 改动不记 rev）。
**对策**：hash 只在事务内、紧接 `UPDATE store` 之前算；提供自检（第十节）重算比对，不一致时**拒绝写入并报 repair**，而不是「顺手修好」——静默修复会把一次真实的 bug 变成一次无法追溯的数据变更。

### R7 「当前边界」取错 → 已压缩的轮次被拉回窗口
窗口身份是推出来的（`turn_no <= 当前 plan 的 epoch_turn`）。`plans` 按 `generation` 追加、允许重压，于是「当前」必须由 **`ORDER BY generation DESC LIMIT 1`** 决定。若图省事写成 `MAX(epoch_turn)`，重压后 `epoch_turn` 不单调（重建时可能把边界往回挪），就会取到旧边界：已经压进冻结块的原文**重新出现在窗口里**，token 直接翻倍，而且没有任何断言会失败——它看起来只是「这次装配比较大」。
**对策**：视图 `v_turn_boundary` 与装配路径统一走最大 `generation`；`plans` 行不提供 delete/update（重压 = 新增 generation，旧行留作对照与审计）。测试用例：连续压两次且第二次 `epoch_turn` 更小，断言窗口按第二次算。

### R8 冷启动并发打开 → 连接被永久标记只读（**唯一一条已压测复现并量化的风险，详见 6.2**）
多个进程同时冷启动打开 WAL 库，抢建 `-shm` 失败的进程会得到 `attempt to write a readonly database`，且**该连接此后每个事务都失败**（实测同一连接重试 25 次全灭），`busy_timeout` 救不了（它只覆盖 `SQLITE_BUSY`）。
**对策**：`readonly` → 丢弃整条连接重开（实测全部自愈）；进程内保持常驻连接；连接初始化做一次 no-op 写探针；拒绝把库放在同步盘/网络盘上。**验收时必须有多进程冷启动用例**——这个 bug 单进程永远测不出来，而它丢的是整段会话的写入。

### R9 单轮正文过大撑爆 WAL
`turn.content` 是原文，一段贴进来的日志可能几 MB。SQLite 页级更新会把整页写进 WAL，几十轮大正文就让 `-wal` 膨胀。
**对策**：本期 `CHECK` 不限长（限长会让合法输入失败）；由装配层在写入前把超过阈值（建议 1 MiB）的正文转存外部 CAS 并只存指针。这条**未实现**，属已知缺口，测试里至少要有一条大正文用例证明不会崩。

### R10 同一 `session_id` 被两个进程驱动（P9 的边界）
存储层**拦不住**这件事，但**症状和"毫无报错"不一样**——初稿这里写错了，本轮的交叉检查把它撞出来：

| 表 | 两个驱动同时写会发生什么 | 会不会报错 |
|---|---|---|
| `turn` | `turn_no` 由 `session.turn_seq` 串行发号（T1），所以**不冲突、无重号、无空洞** | 不报错。后果是**两段对话的轮次被交错**进同一条链，而 P3 的连续性断言**照样通过**——这是它最难发现的地方 |
| `plans` | 两边各自从"没有 plan"算出同一个 `generation`，撞 `UNIQUE(session_id, generation)` | **报错，而且报错是错的方向**（见下） |

`plans` 那条撞上后**不能**走默认的 `约束违反 → StoreCorruptError → F1`：那会把"调用方违反了 P9"诊断成"库坏了"，而两者处置方向相反（F1 说"我兜住了，你不用管"；F16 说"**你做错了，去改进程模型**"）。所以 6.3 为它开了一个例外：`commit_plan()` 里的 `IntegrityError` 译成 **`SessionConflictError` → F16**，判别靠**调用点**而不是错误文本。

**P9 的检出有两条，存储层只负责第一条，第二条要靠会话层**：

| # | 检出方式 | 谁负责 | 当前状态（实测） |
|---|---|---|---|
| ① | `put_plan` 的 **pre-check**：同一 `session_id` + 同一 `generation` 已存在且冻结字节不同 → `SessionConflictError` | **存储层** | ✅ 可命中（实测：`op='commit_plan'`，消息给出两侧 sha16） |
| ①′ | `INSERT` 撞 `UNIQUE(session_id, generation)` | **没有人** | ❌ **不可达**：`BEGIN IMMEDIATE` 全程持写锁，pre-check 与 `INSERT` 之间不可能有第三方提交（实测另一条连接只能拿到 `database is locked`）。所以它不该有翻译分支——`store_sqlite.py` 里那个 `except StoreCorruptError → SessionConflictError` 的真阳性是 0，接到的全是 `CHECK`/`FK`（已报霍承钧删） |
| ② | `read_turn_hashes` 与 `read_turns` **同快照**比对已提交轮次与本进程缓冲 | **会话层**（存储层只交证据） | ⚠️ **未接线**：`session.py` 里零调用点；且 P10 在当前端口形状下满足不了（三之二），接上去就是把不可逆动作建在跨快照证据上 |

**关于「F16 只留一条检出口」（顾图南第三轮裁定），本文提一个反向意见，供终裁：**

> 那条「不可达所以不该给最高信任」的论证，只覆盖 **①′（`INSERT` 撞唯一键）**，不覆盖 **①（pre-check）**。① 实测可命中，且它的证据**不可能由读时序造成** —— 同一 `generation` 上躺着两份不同冻结字节，是事实，不是读序；而 ② 恰恰可能由读序造成，这正是它需要二次确认的原因。
> 用 `ARCHITECTURE.md` §10.6 的标准衡量，**① 是最合格的出口，② 是最不合格的**。只留一条时若留 ②，等于把 F16 唯一入口建在最需要二次确认的那条证据上；并且在 P10 端口形状补齐前，F16 会**零检出力**。
> 本文的建议：**两条都留、信任等级分明** —— ① 确定性、单次即可；② 时序敏感、须同快照 + 二次确认。

> **所以 F16 此刻的检出力是"一条半"**：① 在跑但只覆盖"错开了 generation 之前就撞上"的情形；② 未接线。`ARCHITECTURE.md` §10.6 要求不可逆动作必须二次确认且证据不能由时序造成——**按这个标准，② 在端口形状补齐前不具备检出资格**。这是能力缺口，不是实现缺陷，本期若补不上端口，就只剩 P9 的契约禁止可用。

② 是必要的补充：如果两个驱动恰好错开了 `generation`（一个压完另一个才压），① 不会触发，但两段对话已经把内容混进同一条 `turn` 链了。**这时唯一能发现的信号是"这一轮我明明发的是 A，库里存的是 B"。** 所以 `turn.content_hash` 不是审计字段，它是 P9 的第二个探针。

**② 落地的两个前置条件，缺一条都不许接线上：**

1. **证据必须同快照（P10）。** 正文一次读、指纹另一次读，中间一次外部提交就能让两者错版——而错版之后的结论，**真 P9 与纯读时序在证据上无法区分**。当前端口形状满足不了这一条（见三之二 P10）。
2. **结论必须二次确认（ARCHITECTURE.md §10.6）。** ① 是确定性证据，单次即可；② 依赖"读回来的东西"对"我缓冲里的东西"，天然受读时序影响，所以要求**连续两个快照给同一结论**才成立。

两条**正交，不能互相替代**：P10 说的是"一对证据必须来自同一个快照"，二次确认说的是"这个结论要在两个快照上各成立一次"。前者防错版，后者防偶发——**"多探一次"完全不会顺带修好跨快照**，恰恰相反，二次确认本身就是两个快照，如果每次都各自读正文和指纹，缝会更多。

理由只有一条：**F16 不可逆。** 可逆降级误判一次，下一轮自己就好了；F16 误判一次，这个进程此后永远不写盘，且没有任何信号说明它被误判。

> **排障提示（给人看，不进任何 `if`）：** 看到 `SessionConflictError`，或看到 `StoreCorruptError` 但错误文本是 `plans` 的唯一键冲突（旧实现），第一嫌疑是 P9 被违反。别去跑 `integrity_check`——它一定全绿。

真正的后果仍是同一件事：两个 `Session` 各持一份内存缓冲与 plan 缓存，同一会话产出**两个不同的 payload**，`I1`（同输入同字节）与 `I5` 的前提直接消失。
**对策**：这条只能靠契约禁止（P9）。可选的运行时护栏：`session` 表加一个 `driver_pid`/`driver_heartbeat`，打开时若发现别的进程仍在心跳就拒开——但这引入租约与心跳失效问题（进程被 kill 后租约会悬挂），**本期不做**，靠 P9 的契约 + bench 只开两个 session 来保证。这是「能力上做得到、但语义上不该做」的典型：数据库能串行化写入，串行化不了两个各自持缓冲的驱动程序。

### R11 把审计字段当成有效性闸门
见 4.4 与 `plans` 一节。`plan.store_rev` / `plan.snapshot_id` 若被拿来判 `PLAN_INVALID`，会退化成「每轮重压、每轮全价轮」。**这不是实现 bug 而是设计契约层面的误用**，一旦发生，正确性断言全绿、只有成本静默恶化，最难发现。对策：字段注释写明「审计专用」，不提供任何带「是否有效」语义的方法，测试里对边界轮占比设阈值断言。

---

## 九、已知取舍（诚实声明）

1. **`.ctx` 与 SQLite 不是双写**。库是真相，`.ctx` 是投影。任何「两边都改」的方案都会产生无法裁决的分歧。
2. **不加变更历史表**。`rev` 是一个计数器，不是可回溯的时间线；「rev 12 时这条存不存在」答不出来。需要审计链（哈希链）时另建表，本期不做——做了就得同时定义「谁有权清空历史」，那是另一个决策。
3. **`name` 索引不唯一**，见第三节；代价是库内可能存在同名节点（合法），装配层按 `name_norm` 取时需显式处理多义。
4. **不做原地重排（`--compact`）**，见 R3。
5. **`leaf.kind` 是封闭枚举**。新增叶子种类必须走迁移 + 同步 `LEAF_KINDS` + 同步 `_kind()` 的所有分派点。这是有意的摩擦：让「忘了改一处」在编译/迁移阶段就暴露。
6. **`CHECK` 只兜得住结构，兜不住字符集**。SQLite 无内建正则，V11 的完整控制字符集、限定名字符集仍在应用层。所以**库内约束不能替代 `parse()` 校验**，导入路径必须先过 `parse()` 再过 DDL。
7. **时间字段一律 TEXT，ISO-8601 UTC（`2026-10-28T09:00:00.000Z`）**。字典序即时间序，可跨库直接比，人工可读。禁止与 epoch 整数混用：混用的结果是排序看着对、`BETWEEN` 静默少一半。所有写入走可注入的时钟，保证 bench 可复现。
8. **`ux_leaf_r` / `ux_leaf_e` 比原格式更严**。格式只约束编号唯一，因此一个手写库完全可以有两行完全相同的 `R`；唯一索引不允许。导入这类库时按 `apply_delta` 的既有语义**合并为一条、保留最后一条的标记**并输出 warning——理由是「同 purpose 下同归一化文本」没有第二份信息，而标记冲突在原格式里本来就只有一个赢家（后写覆盖，见 `apply_delta` 的 retag 分支）。代价：仅对「格式合法但语义冗余」的手写库，导入不是字节等价往返。**这一条是刻意选择「宁严不乱」**：放开唯一约束换取导入保真，就等于把并发幂等这条主线交给每次写入自己保证，风险不对等。

---

## 十、自检与验收

### 10.0 叙述性规范的判据：删掉它，哪个测试会红？

本文有三类规范，最小完整度要求不同：

| 类 | 例 | 最小要求 | 失效形态 |
|---|---|---|---|
| **清单** | 审计字段（在 ARCHITECTURE.md §2.2） | 可举例 + 显式标注"不维护" | 少一项 = 漏查 |
| **枚举** | 编号命名空间（在 ARCHITECTURE.md 附录 A.0） | 必须逐行齐全 | 少一行 = 有人往空号上放新概念 |
| **叙述** | 4.2 前像格式、4.5 断言位置、第五节事务边界 | **必须挂一个可执行检查** | 少写一条规则，读者永远不会发现 |

第三类没有天然的机械形式，所以**不许只写成散文**——给每条叙述挂一个"删掉就会红"的检查，它才可判：

| 叙述 | 挂在哪个检查上 | 少写一条会怎样 |
|---|---|---|
| 4.2 前像的字段顺序与分隔符 | 10.1 末的「按前像重算 → 与库内 `body_hash` 比对」 | 少一条字段规则 → 重算结果不符 → 当场报 repair |
| 4.5 两处断言的挂载层级 | 10.3 用例 6（删中间一轮 → 必须降级） | 断言被删/上移 → 用例 6 不降级 → 红 |
| 第五节事务边界的"provider 返回后才取号" | R4 的慢 provider 注入（断言锁持有时间只与本地写有关） | 在事务内 `await` → 锁持有时间随网络膨胀 → 红 |

**判据**（与"把不变量删掉，触发条件还成立吗"同一个手法）：**删掉这条叙述里的某一行，有哪个测试会红？一个都没有 → 它只是注释，不是规范。** 注释可以少写，规范不行，而两者在文档里长得一样——这正是这类遗漏从不出现在 review 里的原因。

**但它的输入不是文档自身，是「文档 × 测试套件」**（顾图南指出）。可判性来自"删一行 + 跑一次测试"，成本比纯 grep 高一个量级。**所以它报红时不能二分，要三分：**

| 情况 | 处置 |
|---|---|
| 这条叙述**没有**对应测试 | 补测试；补不出测试就把它**降级成注释**（去掉"必须/不得"的语气） |
| 有测试，但删掉这行**依然不红** | 多余规则，删 |
| 有测试，删掉这行**也红** | **先别急着说它多余**——看那个测试是不是**同时覆盖着别的叙述**：是 → 标归属或拆测试；否 → 才是多余规则 |

第三支最容易漏，因为"多条叙述共用一个测试"在文档里看不出来。例：4.5 的「两处断言常开」与「不得上移到 `open()`」都由 10.3 用例 6 覆盖——删掉前者，用例 6 仍可能因后者而红，于是被误判成"前者多余"。**它不是多余，是归属没写明**；而归属未写明，下一个人改测试时就不知道自己在拆哪条规范的护栏。

**因为跑测试太贵，再配一条只跑 grep 的同伴检查——它兜的是上一条看不见的那类漂移：**

> **同一个公式或结论在文档里出现第二次，就是漂移点。** 一次 grep 就够，不需要跑测试。

两次引用之所以危险，是因为**旧结论最容易活下来的地方正是"第二次引用它"的那一处**——第一次写的时候作者在推导，第二次写的时候作者在引用，引用时不重算。它查得到、且读起来完全正常（因为它曾经正常过），比悬空引用危险一档。

grep 命中后必须**当场归类**，否则等于没查：

| 归类 | 处置 | 本文实例 |
|---|---|---|
| **现行定义** | 保留；若与其他命中不一致 → 以更早的推导为准并改另一处 | 4.4 的 `snapshot_id` 派生式 |
| **历史记录** | 保留，但必须**显式标注"这是被废掉的写法"** | 4.4 里那条初稿公式，已标"被 P8 打掉" |
| **陈旧残留** | 删。它没有任何存在理由 | 无（本轮 grep 已清） |

修完之后，全文该模式**恰好命中 1 处**，且是"历史记录"类。**没有第二处**。若将来变成 2 处以上，两种可能：要么有人重新捡起了废掉的公式，要么有人又在引用处重写了一遍定义——两者都该在 review 里被这条 grep 挡住。

#### 10.0.1 检查项的自述会污染检查（本节已连续踩四次）

**这条检查有个坑：检查项的自述本身会命中检查。** 我第一版把被判定的那个公式原样写在小节里，grep 立刻从 1 处变成 3 处——多出来的两处是"在讨论这条公式"，不是"在使用这条公式"。改成自然语言之后，同一段的**结语**「复验 0 处未带文件名的 `§`」又命中一次；再改才归 0。

三次都是同一个动作：**一边宣布这条检查，一边在宣布的句子里制造它要抓的东西。** 而且每次改完都"看起来干净了"——直到又跑了一次脚本。

所以这不是"要更小心"：**规则正文是这个检查的固定污染源**，它天生要提到被扫目标，且提得越精确越好。偶发笔误能靠小心避免，固定污染源不能——**它的处置只能是给检查加结构，而不是给措辞加小心。**

**第四次就发生在把这条检查写成脚本的当天**：10.0.2 里那句「派生式命中 4 处」又捎上了被扫字面量，脚本当场报红（区外一处命中、无指针）。前三次是人眼看出来的，这一次是机器 —— 差别不在我变得更小心了，而在污染源终于有了一个不由我控制的检测器。这也是「豁免要枚举、执行要可跑」的真正理由：只要检查还停在「我会记得查」，它就会稳定地漏掉写规则的那一段。

> **通则：检查项的文档必须与被检查的目标在字符串上可分。**

处置有两条，本文用的是后者：

1. 把检查限定在**标题区段**（如只看代码块、只看 4.x），自述留在正文——粗但有效，缺点见 10.0.2 的"盲区"；
2. **自述改写为不含目标字面量的说法**——本文现在写"那条初稿公式""`body_hash` 取前 16 位"，不写原文。

第 2 条里还有一个选择：用占位符（`I⟨n⟩`）还是用自然语言（"那条初稿公式"）。分界是**读者是否需要精确定位**：

| 读者要不要知道"是哪一条" | 用哪种 | 例 |
|---|---|---|
| 不需要，只要知道"这里指某条不变量" | **占位符**更省读者 | 反例段落里的 `I⟨n⟩` |
| 需要，否则看不懂它为什么留在这里 | **自然语言 + 指出原文位置** | 本文："那条初稿公式"，原文在 4.4 |

用错的代价不对称：该用占位符却写了自然语言 → 读者多读几个字；该用自然语言却写了占位符 → 读者**不知道指的是哪条**，只能去猜或跳过后半段论证。所以不确定时偏向自然语言化。

#### 10.0.2 检查器必须有作用域，而且作用域有边界

**跨文档引用一律写 `FILE.md §N.M`**，检查器跳过紧跟 `.md ` 之后的 `§`。不这样写，「小节号必须指向本文档存在的小节」这条检查会把别人的小节号打成假阳性——而**假阳性会训练人忽略检查**，这与自述污染是同一个病的两面（一个是自己写进去的噪音，一个是别人带来的噪音）。本文已按此约定把全部跨文档引用改成带文件名的形式。

但"跳过讲规则的那一节"这个说法**太宽**（顾图南修正）。能不能整节豁免，取决于那一段**是不是纯规则正文**，不取决于它叫什么：

| 区段 | 性质 | 可用的豁免粒度 |
|---|---|---|
| 本文 10.0 | **纯规则区**：只描述检查，不描述系统 | 可整节豁免 |
| ARCHITECTURE.md §2.2 / 附录 A.0 | **混合区**：既有使用说明（规则正文），又有「审计字段登记」表（**实质内容**） | **只能标记级**，那张表不豁免 |

那张登记表必须继续被扫，理由很实在：**违规最容易藏在那里，因为它长得跟规则区一模一样。**

**自检脚本已经可执行：`tools/check_doc.py`。** 它做三件事——裸 `§`（未带文件名）、围栏平衡、现行公式的每一处命中能不能归类（定义区恰好 1 处，区外每处必须带指针）。当前结果：裸 `§` 0、围栏 14 平衡、派生式的命中全部可归类（定义区恰好 1 处，区外每处都带指针）——具体计数不写进正文：写死一个数字等于给自己埋一条必然会烂的断言。

**豁免在脚本里是一张打印出来的表，不是散落的 `if`。** 两条：整节跳过 10.0（纯规则区，它必须能自由引用被扫字面量才讲得清）、`FILE.md §` 形态不算裸小节号（跨文档引用，本文无权校验别人的小节是否存在）。**每次运行都先把这两条印出来**——因为藏在代码里的 `if` 等于没登记过的豁免，而没登记过的豁免连"我豁免了什么"都没人知道。

> **豁免被枚举 = 可以被 review；豁免藏在分支里 = 只可以被继承。** 这与"隐式豁免比显式豁免更坏"是同一条：坏了还能被发现的那个，才叫豁免。

**还有一处我要自己拆掉：** 本小节原写"被废公式残留 1 处（可 grep 命中）"。**这句现在不成立**——那条初稿公式已按 10.0.1 改成自然语言（"那条初稿公式"），grep 命中 0。也就是说 10.0 的两个手段在这里互斥：**自然语言化消除了自述污染，同时消灭了 grep 的可能性**。所以这条守卫已从"可 grep"降级为"只能人读"，我把它如实标出来，而不是留着一句读起来很安全、实际查不到东西的话。


三级锚，按可用性排：

| 锚 | 可用性 |
|---|---|
| 行首标记（`❌` / `✅` / 围栏内） | ✅ **首选**：局部、稳定、**写入时就存在** |
| 标题区段（整小节跳过） | ⚠️ 可用，但范围大，容易把真违规一起盖住 |
| 行号区间 | ❌ **禁用**：改一行就漂，而漂移不报错 |

两条约束：

1. **作用域不得覆盖「最需要被扫的区域」。** 不是"尽量小"，是**必须小于一个具体的东西**——这样它才可判，而不是一句态度。
2. **锚必须落在作者写入时就顺手留下来的东西上**（标记、围栏、标题）。行号是读者事后算出来的，作者从未写过它，所以它不稳定，**且不稳定时不会报错**。

**盲区比例外表更坏。** 例外表每次命中至少还提醒你一次（哪怕是噪音）；**盲区什么都不说**。所以拿不准时**不豁免**——这与占位符那条一样是代价不对称：误报会自己冒出来、可以事后收紧；漏检不会自己冒出来。

#### 10.0.3 清单的键必须是判据的输出

**审计字段登记表每一行都该"因为它满足判据"才在表里，而不是"因为它名字像审计"。** `turn.content_hash` 是这条的完整反例：名字 100% 像审计，判据 100% 判它不是（已提交轮次的内容**不该**合法变化，变了就是异常，所以它必须参与控制流，见 R10）。**一个例子同时证明正反两面，比两个例子各证一面强。**

### 10.1 自检 SQL

```sql
-- ① 引用完整性
PRAGMA integrity_check;
PRAGMA foreign_key_check;

-- ② 键树计数（V13 / V17 的库内对照：T / P / R / E 必须与头部一致）
SELECT
  (SELECT COUNT(*) FROM topic t WHERE t.store_id = :s AND t.state = 'live') AS n_t,
  (SELECT COUNT(*) FROM purpose p JOIN topic t ON t.id = p.topic_id
    WHERE t.store_id = :s AND p.state = 'live' AND t.state = 'live')        AS n_p,
  (SELECT COUNT(*) FROM leaf l JOIN purpose p ON p.id = l.purpose_id
     JOIN topic t ON t.id = p.topic_id
    WHERE t.store_id = :s AND l.state = 'live' AND l.kind = 'R')            AS n_r,
  (SELECT COUNT(*) FROM leaf l JOIN purpose p ON p.id = l.purpose_id
     JOIN topic t ON t.id = p.topic_id
    WHERE t.store_id = :s AND l.state = 'live' AND l.kind = 'E')            AS n_e;

-- ③ 编号分配器与真实行是否一致（不一致说明有人绕过分配器插了行，或被物理删过）
SELECT a.kind, a.parent_num, a.last_num, MAX(x.num) AS max_row
FROM num_alloc a
LEFT JOIN (
    SELECT 'T' AS kind, 0 AS parent_num, num FROM topic WHERE store_id = :s
    UNION ALL
    SELECT 'P', t.num, p.num FROM purpose p JOIN topic t ON t.id = p.topic_id
     WHERE t.store_id = :s
) x ON x.kind = a.kind AND x.parent_num = a.parent_num
WHERE a.store_id = :s
GROUP BY a.kind, a.parent_num, a.last_num;

-- ④ 幂等归并是否有漏（同 purpose 下重复的活 R；正常应返回 0 行）
SELECT purpose_id, text_norm, COUNT(*) AS c
FROM leaf WHERE kind = 'R' AND state = 'live'
GROUP BY purpose_id, text_norm HAVING c > 1;

-- ⑤ 两臂是否共享同一 transcript（同一 transcript 必须有且只有 A、B 各一臂）
SELECT transcript_hash, GROUP_CONCAT(arm) AS arms, COUNT(*) AS n
FROM session GROUP BY transcript_hash HAVING n <> 2 OR arms NOT IN ('A,B', 'B,A');

-- ⑥ system 首段是否逐字节两臂相同（口径审计，非计费）
SELECT a.turn_no, a.system_hash, b.system_hash
FROM v_measure a JOIN v_measure b
  ON b.transcript_hash = a.transcript_hash
 AND b.turn_no = a.turn_no
 AND a.arm = 'A' AND b.arm = 'B'
WHERE a.system_hash IS NOT NULL AND a.system_hash <> b.system_hash;

-- ⑦ 轮次连续性（ARCHITECTURE P3：有洞即 StoreCorruptError）。
--    MAX = COUNT(DISTINCT) 即「1..N 无空洞」，一个 UNIQUE 索引即可覆盖整次判定。
SELECT session_id,
       MAX(turn_no)             AS hi,
       COUNT(DISTINCT turn_no)  AS n
FROM turn GROUP BY session_id HAVING hi <> n;

-- ⑧ plans 与 event 是否配平（每次压缩都该有一条 compaction 事件，反之亦然）
SELECT p.session_id, p.generation, e.generation AS ev_gen
FROM plans p
LEFT JOIN event e
  ON e.session_id = p.session_id AND e.kind = 'compaction'
 AND e.generation = p.generation
WHERE e.id IS NULL;

-- ⑨ 是否存在「没有 plans 行却被当成已冻结」的轮次（窗口身份自洽）
SELECT b.session_id, b.turn_no, b.window_state, b.boundary_turn
FROM v_turn_boundary b
WHERE b.window_state = 'frozen' AND b.boundary_turn IS NULL;
```

`body_hash` 自检必须在应用层做（SQLite 无 sha256）：按第五节前像重建 → 与 `store.body_hash` 比对；不等则**拒绝写入并报 repair**，不做静默修复。

### 10.2 并发验收用例（给测试）

这四条必须都过，且**四条都只能在多进程下测出来**（单进程永远是绿的，这正是它们容易被漏测的原因）：

| # | 用例 | 断言 |
|---|---|---|
| 1 | N≥4 进程 × M≥20 事务，**同时冷启动**，各自写独有条目 | 每个进程成功数 = M；总量 = N×M，一条不丢。记下 `readonly` 自愈次数（>0 是正常的，说明防御生效） |
| 2 | N 进程并发 upsert **同一条** R | 该文本只有 1 行活叶子；总行数不随进程数增长 |
| 3 | N 进程并发取 T 编号 | 编号无重号、无空洞；`num_alloc.last_num` = `MAX(num)` |
| 4 | 一进程连续写，另一进程连续 `render` | 每次输出都能被 `parse()` 通过，且 `body_hash` 与输出内容自洽（不自洽即 R1 的混合快照） |

第 1 条必须**在干净目录上跑**（先删 `-wal`/`-shm`），否则复现不出竞争——这也是它容易被漏测的原因。

**第 1 条同时是「实现要求」，不只是验收用例**（team-lead 拍板）。这个 bug 决定了实现必须做两件事，缺一件这条用例必败：

1. **连接级错误分类**：区分「可重试」（`SQLITE_BUSY` / `LOCKED`，在 `busy_timeout` 内交给 SQLite 自己等）与「连接已废」（`SQLITE_READONLY`，**粘在连接上**，实测同一连接重试 25 次全灭）。分类状态挂在**连接对象**上，不是挂在语句或事务上。
2. **连接重建路径**：捕获 `readonly` → 标记该连接待回收 → **下一次新写事务开始前** `close()` 再重开。热路径内不重试、不重放（6.3）。

**只做 `busy_timeout` 重试是不够的**——25/25 全灭就是证据：那不是 `SQLITE_BUSY`，等多久都不会变可写。加超时、加重试次数、加重试间隔，全都无效。这条写在这里，是因为"顺手加重试"是最省事也最自然的反应，而它恰好完全无效。

### 10.3 语义验收用例（零成本，纯断言；防的是"能跑但错"）

这一组不涉及并发，但**比并发用例更容易失守**：它们全部通过时，性能可能已经坏掉一个数量级，且没有任何报错。

**凡是断言降级的用例，必须断言 `(degraded, degrade_reason)` 二元组**（`ARCHITECTURE.md` §10.7）。只断言 `degraded` 的用例不算降级用例 —— 它只证明「出了点事」。理由很实在：**「降级为真但原因错」是唯一一种所有测试都会绿的失败形态**：凡是断言降级的用例都满足，而在没被坑过之前，没人会去断言「应该是什么原因」。

| # | 用例 | 断言 |
|---|---|---|
| 1 | 连续 `commit_plan` 三次（键树一字未改） | `store.rev` 三次相等、`store.body_hash` 三次相等（**I16**，见 4.6） |
| 2 | 同文本 + 同标记连续 `attribute()` 100 次 | `rev` 不增；`leaf` 仍只有 1 行活记录（P4 + R2） |
| 3 | 同文本 + 不带标记 `attribute()`（此前有 `[钉住]`） | 标记仍是 `[钉住]`，**不得被清空**（R2 的 `COALESCE`） |
| 4 | 静态检查：`plans.snapshot_id` / `plans.store_rev` / `store.updated_at` 的所有使用点 | 只允许「写入」与「原样输出」，**不允许任何“比较后抛错/降级”**（A7 / R11） |
| 5 | 连续压两次且第二次 `epoch_turn` 更小 | 窗口按**第二次**算（R7，走 `ORDER BY generation DESC LIMIT 1`） |
| 6 | 人为删掉 `turn` 中间的一轮（模拟外部破坏），再装配一次 | `read_turns` 抛 `StoreCorruptError` → 降级 `buffer_only` 并记 `STORE_CORRUPT`（**F1**）；**不得**在 `open()`/构造期抛出——那是 F2，不是本设计的挂载点（4.5） |
| 7 | `put_plan` 传一份 `r_first_turn = epoch_turn + 5` 的计划（**故意违反 `CHECK`**） | 必须抛 `StoreCorruptError`（F1：压缩算错了）。**不得**抛 `SessionConflictError` —— 后者是 F16 专用，会把「压缩少算一位」诊断成「你有两个驱动」，并让会话此后**永久停写**。同理 `plans` 的 `CHECK` / 外键违反一律 `StoreCorruptError`，只有 `UNIQUE(session_id, generation)` 译 F16（6.3）。**本条当前是红的**：实现的 `except StoreCorruptError` 把同方法内的一切约束违反都吞成了 F16，修法见 6.3 末段 |
| 8 | 两次调用之间插一次外部提交：先 `read_turns(1,3)`，改第 3 轮，再 `read_turn_hashes(1,3)` | **要么**端口上存在同时交出正文与指纹的组合入口（一次事务取完），**要么**两个方法接受同一个 `StoreTxn`。当前形状两者都没有 → 本条是**契约缺口**，不是实现缺陷（三之二 P10）。在补齐之前，F16 检出② 不得接入（`ARCHITECTURE.md` §10.6：不可逆动作要求更硬的证据） |

第 7 条不是假想：它在复核里**实测到了**（构造一份越界的 `r_first_turn`，得到 `concurrent_driver` + `store_writable=False`，且此后每一轮都带这个原因）。这类错误的形状是「一个真 bug 借用了一条更响亮的诊断」，比漏报难查得多 —— 漏报至少还会让人怀疑存储层，误诊会把人直接支使去查一个不存在的进程。

第 4 条建议做成 CI 里的 grep 级检查而不是人来 review——**「审计字段不得参与控制流」这条纪律，靠人记住是不可能的**。

**判据不在本文维护**：审计字段清单、违规形态（`if <审计字段> ...: raise|降级`、或出现在 `PLAN_INVALID` 判定链上）、允许形态（仅"写入"与"原样输出"）、以及**豁免必须显式登记（不登记即违规）**，全部以 **ARCHITECTURE.md §2.2** 为准，字段登记在 **ARCHITECTURE.md 附录 A.0**。本文只引用、不复制——两份清单必然漂移，而漂移的表现是"检查还在跑，只是少查了一个字段"，不会有任何报错。

三个字段里 `store.updated_at` 最容易被漏：它至今没被任何人拿去做判定，但**"上次更新早于本次边界 ⇒ plan 过期"是个听起来像常识的推断**，而常识型错误恰恰没人 review。
