# transcript 格式

bench 的唯一输入是 transcript（剧本）。格式刻意做窄：**只描述对话，不描述上下文管理**——
两臂的差别必须全部由代码产生，不能藏在数据里。

## 文件

- `bench/transcripts/<name>.jsonl` —— 对话本体
- `bench/transcripts/<name>.meta.json` —— 元信息

## JSONL 每行一个事件

```json
{"turn": 1, "role": "user", "content": "把 render() 里 E 行的缩进改一下"}
{"turn": 1, "role": "assistant", "content": "已定位到 _fmt_item()，改 4 格缩进。"}
{"turn": 2, "role": "user", "content": "顺带查一下 validate 会不会漏掉这种情况"}
```

字段约束：

| 字段 | 必填 | 说明 |
|---|---|---|
| `turn` | 是 | 轮次序号，从 1 连续 |
| `role` | 是 | `user` / `assistant` / `tool` |
| `content` | 是 | 纯文本。**不含任何装配指令、不含「请只读某文件」这类提示** |
| `pinned` | 否 | `true` 表示该条含硬约束，装配时进冻结块 |

一轮 `user` 之后的所有 `assistant`/`tool` 行都属于同一轮。**评分以 `turn` 归组**。

## meta.json

```json
{
  "name": "t1-project-evolution",
  "description": "围绕同一个仓库连续 40 轮开发，多次回到早前决定",
  "corpus": ["../context-lattice/references/store-format.md", "..."],
  "corpus_tokens": 94233,
  "locality": "good",
  "script_source": "scripted-from-real-worklog",
  "script_basis": "按 2026-10 期间该仓库的真实改动顺序编排，用户侧措辞据工作日志重写",
  "notes": "第 12、27 轮回头引用第 3 轮的决定，这是本 transcript 构造的关键点"
}
```

`locality` 只允许三个值，**每个值至少要有一份 transcript**：

| 值 | 含义 | 期望结果 |
|---|---|---|
| `good` | 每轮只需局部素材，且会回访早前结论 | B 臂明显省 |
| `bad` | 每轮工作集接近全量 | B 臂衰减甚至转负 |
| `none` | 单轮 / 极短会话 | **B 臂更贵**（冻结块是净开销） |

`script_source` 只允许 `captured`（真实采集）或 `scripted-*`（编排）。
**报告标题依赖这个字段**：非 `captured` 一律不得称"生产实测"。

## 为什么不做成"直接存两臂的 payload"

因为那样等于把答案写进题目：只要有人手工调整了 payload，测出来的就是人的手艺，不是方案本身。
transcript 只放对话，两臂各自从同一份对话生成 payload —— 这是本 bench 有效性的唯一支点。

## 装配纪律：transcript 必须能只靠自己回放出 payload

两臂的 `system_block()` 都通过同一个 `make_system(text) -> SystemPrompt` 构造器，
**S 段正文由 bench 统一提供且逐字节相同**；transcript 只提供"第几轮、谁说了什么"。

因此 transcript 里**不得**出现：系统提示正文、预算数字、边界轮次、期望的 payload 长度。
**一条判据：把这个 jsonl 交给第三方，他应当能只凭它和 SDK 重建出同一串 payload。**
做不到就说明剧本里混进了答案。

## 归因增量放哪

`attribute()` 的输入（T/P/R/E 行）不是对话，但它是真实工作流的一部分，所以：
- **允许**放在 `<name>.attrib.jsonl`，一行一轮：`{"turn": 12, "lines": ["T 存储格式", "  P1.1 事件节点"]}`
- **不允许**放进 `.jsonl` 的 `content` 里 —— 那会把"模型标注的内容"和"用户说的话"混成一锅，
  冻结块的 Tier 1/2 就分不清该拿哪个。

没有 `attrib.jsonl` 时，LatticeArm 退化为"库为空"路径（`on_empty_store=no_compact`），
B 臂会接近 A 臂 —— 这是**预期行为**，也是 `bad` / `none` 两档的天然形态。

## 一轮的边界

`turn` 是**装配单位**，不是消息序号：一轮 = 1 条 user + 其后所有 assistant/tool 行。
「最近 K 轮」指的是 K 个 turn，不是 K 条消息 —— 否则 K 的单位会随工具调用条数漂移。
