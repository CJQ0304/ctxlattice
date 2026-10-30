# ctxlattice

**给对话里的约定一个可反复读写、还能被程序校验的持久形态 —— 并且接管上下文装配，而不是让历史一直堆在窗口里。**

这是 [context-lattice](https://github.com/CJQ0304/context-lattice) 技能包的 **C1 形态 SDK**：
技能包是「模型每轮自己去读库」，这里是「库直接产出 provider 可发送的 payload」。

```
传统做法：[ system ][ 第1轮 ][ 第2轮 ] … [ 第N轮 ][ 本轮 ]     ← 每轮整包重发，线性增长
ctxlattice：[ S ][ F 冻结块 ][ R 边界内原文 ][ U 本轮 ]         ← 由存储装配，两次压缩之间只追加
```

## 它解决什么

上下文管理的默认做法是把全部历史每轮重发一遍。代价随轮数线性增长，而其中大部分内容在绝大多数轮次里根本用不上。

`ctxlattice` 把对话拆成三层键树（事物 → 目的 → 要求）存起来，装配时只放三样东西：
- **S** 静态系统提示（会话内不变）
- **F** 冻结记忆块（钉住项 / 约束 / 改口轨迹 / 骨架索引 / 相关子树 / 事件节点指针）
- **R** 自上次压缩边界起累积的原文轮次

然后 **只在本轮用户消息尾部追加**。两次压缩之间请求是纯追加的，所以 provider 的 prefix cache 能命中。

## 三条设计红线

| # | 红线 | 为什么 |
|---|---|---|
| **I1** | 非边界轮的 payload 必须以上一轮为**字节前缀** | 一旦每轮重写，缓存全失效，省下的 token 钱不够赔缓存损失，而且**不会报任何错** |
| **I2** | `assemble()` 不读存储、不读时钟、不读环境、不读随机 | 确定性靠结构保证，不靠纪律。它只接受已经取好的数据 |
| **I3** | `send()` 永不抛异常 | 单轮装配任何故障都降级为「全量历史直发」；但**配置错误在构造期大声失败** |

## 它明确不做什么

- 不做语义理解。T/P/R 怎么拆由调用方（模型）判断，SDK 只校验和持久化。
- 不做向量检索、embedding、相似度召回 —— 那些不可复现。
- 不做多租户、鉴权、HTTP 服务。它返回 payload，发请求是调用方的事。
- 不承诺「记得更多」。它的收益是**回到同一件事的成本**更低，不是记忆容量更大。

## 边界（必须一起读）

- **当轮不省**。原文还在窗口里，收益从它离开窗口那一刻开始。
- **工作集接近全量时反而更贵**。收益来自任务的局部性，不来自这套方法。
- **短会话更贵**（约 `L < 2N` 轮）。冻结块是净开销，没有回本空间。
- 已发出的历史消息不是「读取源」，替换不了。它能救的只有跨会话、长会话被压缩或截断、窗口吃紧要主动卸载这三种情形。

## 安装

```bash
pip install git+https://github.com/CJQ0304/ctxlattice.git
```

核心包零第三方依赖（只用到标准库）。要跑 `mode=exact` 的精确计数再加一个 extra：
`pip install "ctxlattice[bench] @ git+https://github.com/CJQ0304/ctxlattice.git"`。

## 快速开始

```python
from ctxlattice import Session, Policy, SqliteStore

def call_provider(messages):
    """占位：换成你自己的 provider 调用（OpenAI / Anthropic / 本地模型都行）。"""
    assert messages[0]["role"] == "system", "S+F 走单条 system（Framing.SINGLE_SYSTEM）"
    return "已定位到 render.py 的 _fmt_item()。"

store = SqliteStore("session.db", store_id="demo", session_id="s1")
session = Session.open(store=store, policy=Policy(budget_tokens=8000))

payload = session.send("把 E 行的摘要规则再讲一遍")
session.observe(call_provider(payload.to_messages()))

# 模型给出本轮的结构化归因（SDK 只校验与落库）
session.attribute(["T 存储格式", "  P 事件节点", "    R 摘要禁半角竖线 [约束]"])
session.close()
```

> 上面这段是**被 `tests/test_readme_example.py` 逐字执行过的** —— 它从本文件里把这段
> 代码块抽出来 `exec`，不是"照它另写一份"。所以改这段而没跑通，套件会红。
> 为此 `call_provider` 必须是本地桩：留一个未定义的 `client` 会让"整段可执行"永远做不到，
> 而做不到就等于没人验。
>
> 两处容易照抄错的地方：类名是 `SqliteStore`（不是 `SQLiteStore`）；归因行是 `P 事件节点`
> （编号由 SDK 分配，写 `P1.1 事件节点` 会被 `AttributionError` 拒掉 —— 那是**刻意的**，
> 调用方不指定编号）。

## 测试与自检

```bash
python -m unittest discover -s tests          # 129 个用例
python tools/check_doc.py                     # 文档自检：裸 § / 围栏平衡 / 公式命中可归类
```

## 测量

`bench/` 用同一份 transcript 跑两臂（全量重发 vs 本 SDK），逐轮报三个数：
**输入 token 降幅**、**扣除缓存折扣后的等效成本降幅**、**回本点**。

```bash
python -m ctxlattice.bench                    # 默认写 bench/results/<日期>-<label>
python -m ctxlattice.bench -h                 # 全部开关
```

> **解释器决定口径，必须挑对。** 装了 `tiktoken` 才是 `mode=exact`；没装会**静默**退到
> `mode=estimate`（chars/2）—— 不报错、不少一行，只是换了个人算，而且**连正负号都可能不同**。
> 跑完先看 `summary.json` 里的 `mode`，三份都必须是 `exact` 才能对外引用数字。

口径与禁止声称的结论见 [`docs/design/MEASUREMENT-SPEC.md`](docs/design/MEASUREMENT-SPEC.md)。
实测结果见 [`bench/results/`](bench/results/)。

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/design/ARCHITECTURE.md`](docs/design/ARCHITECTURE.md) | 架构、API 签名、失败矩阵、不变量 |
| [`docs/design/MEASUREMENT-SPEC.md`](docs/design/MEASUREMENT-SPEC.md) | 计费三层口径、反例要求、禁止的结论 |
| [`docs/design/SCHEMA.md`](docs/design/SCHEMA.md) | SQLite DDL、事务边界、并发模型 |
| [`docs/design/STORE-SCHEMA.md`](docs/design/STORE-SCHEMA.md) | 表结构与列的逐条口径、P1–P10 存储侧不变量 |
| [`docs/design/TRANSCRIPT-FORMAT.md`](docs/design/TRANSCRIPT-FORMAT.md) | bench 输入格式（只放对话，不放装配指令） |

## 许可

MIT
