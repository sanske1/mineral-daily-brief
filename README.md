# 矿权日报 Agent

按 MCP 协议取数、用 LangGraph 两节点编排的矿业分析助手。带一个网页界面。

`python web.py` 起网页（左边对话、右边数据预览），或者 `python agent.py` 用命令行聊天。
问「Pilbara 锂矿的储量和最近锂价怎么样」，它会自己去检索新闻、下载年报抽储量表、查行情，
然后带着出处回答。

> 上手跑：[RUN.md](RUN.md)

---

## 交付物对照

| 题目要求 | 位置 |
|---|---|
| 3 个 MCP server | [news_server.py](news_server.py) · [pdf_server.py](pdf_server.py) · [price_server.py](price_server.py) —— 共 9 个工具 |
| Agent 编排 | [agent.py](agent.py) —— LangGraph 两节点（agent + toolnode） |
| 网页界面 | [web.py](web.py) + [index.html](index.html) —— 对话 + 数据预览 |
| `mcp-config.json` 可接 Claude Desktop / Cursor | [mcp-config.json](mcp-config.json) |
| `RUN.md` 5 分钟跑起来 + 一条 docker-compose | [RUN.md](RUN.md) + [docker-compose.yml](docker-compose.yml) |

**6 个 Python 文件 + 1 个页面**：一个缓存层 + 三个 server + 一个 agent + 一个网页后端。
网页只用 starlette + uvicorn（fastmcp 已带进来），没有新依赖，也没有前端构建步骤。
三个 server 都用 **FastMCP** 封装，都不调用 LLM、不依赖外部服务（没有向量库、没有嵌入模型），
所以每一个都能单独挂进 Claude Desktop 跑 —— 这是 `mcp-config.json` 那条要求的前提。

---

## 编排：LangGraph 两个节点

```
START ──► agent ──(有 tool_calls)──► tools ──┐
            │                                │
            └──(没有)──► END                 │
            ▲                                │
            └────────────────────────────────┘
```

`agent` 节点把消息历史交给绑定了工具的模型；`tools` 节点是 LangGraph 的 `ToolNode`，
执行模型要调的工具。工具在启动时从 3 个 MCP server 一次性加载。

代码就是 `agent.py` 里的 `build_graph()`，二十来行。

### 工具怎么从 MCP 到 LangGraph

用 FastMCP 的 `Client` 拉起 3 个 server，`list_tools()` 拿到 9 个工具，
再逐个包成 LangChain 的 `StructuredTool`（`make_tool()`）——
用 MCP 的 `input_schema` 动态生成一个 pydantic 模型当 `args_schema`。

工具名是 FastMCP 给的 `{server}_{tool}`（如 `lme-price_get_price`），
这个格式本来就是合法的 OpenAI 函数名，不需要改写。

> ⚠️ **为什么不用官方的 `langchain-mcp-adapters`**：它要求 `mcp<2.0.0`，
> 而 fastmcp 4 要求 `mcp>=2.0.0`，两者**硬冲突**。所以工具转换是手写的 20 行。

---

## 三个 server 的取数设计

### mining-news — 两条互补通道

| 通道 | 来源 | 覆盖 | 正文 |
|---|---|---|---|
| A 常驻 RSS | mining.com、The Northern Miner | 全站最新，翻页可回溯数年（实测 632 篇） | ✅ 可抓全文 |
| B 查询 RSS | Google News（中英各一路） | 按查询实时生成 | ❌ 见下 |

**为什么要两条**：通道 A 是「全站最新」，某个具体标的（比如 Pilbara）可能几十天都没上过头条 ——
实测近 52 天语料里 `Pilbara` 出现 **0 次**。所以检索做成阶梯：

```
本地精确召回(AND) ──不足 3 条──► 按查询实时抓 Google News ──重试──►
   └──仍无──► 宽松召回(OR，标注低置信) ──仍无──► 放宽时间窗（标注不是近期新闻）
```

**顺序不能反**：先宽松召回再补抓的话，OR 凑出的几条弱相关结果会让上游误以为「已经召回充分」，
从而跳过真正该取的数据源。这个坑实际踩过一次。

**中文可检索**靠两件事：FTS5 索引做 CJK 逐字空格化（`锂矿` → `锂 矿`，用短语查询等价子串匹配），
加上一份中英术语词典做跨语言扩展（`锂矿` → `OR lithium OR spodumene`）。都是确定性词表，不调模型。

**Google News 只给标题不给正文**：它的链接**不会**重定向到发布方原文（实测跟随重定向后
停在 `news.google.com` 自己的 592KB JS 页面上）。所以这些条目标记 `origin=gnews`，
`fetch_article` 遇到会明确返回 `full_text_unavailable`。**宁可承认拿不到，也不编。**

### mineral-pdf — 按坐标重建表格

不用 `pdfplumber.extract_tables()`：实测两份真实报告都不行 ——
无框线的（Implats AIF）返回 **0 张表**，有框线的（Pilbara 年报）检测到了但**标签列被丢掉**。

改成**数据驱动定列**：

1. 词按纵坐标聚成行 → 找出含 `Tonnes` + `Grade` 的表头行
2. 只扫「含分类词且有数字」的行作为数据行
3. 把这些行里**数字的横坐标聚类成列** ← 关键
4. 每列取横向邻近的表头词判定语义（吨位/品位/金属量）与单位

第 3 步是关键。真实报告里表头词挨得极近（Pilbara 相邻列中心只差 40pt），
按表头词间距合并会全部糊成一团；按数据数字的横坐标分列则天然分开。

**抽不到就 abstain**：返回 `status=no_table_found` 和原因，不返回半成品，
更不拿正文段落里的数字凑表。表里没有金属量列时用 `吨位 × 品位` 推算，
但结果放在 `contained_derived` 字段里与抽取值分开 —— 抽到的是原文事实，推算是我们的算术。

### lme-price — 官方优先 + 双源冗余

| 品种 | 源 | 权威性 |
|---|---|---|
| 沪铜 / 沪锌 / 沪镍 | 上期所官网 | **official**，支持历史 |
| 碳酸锂（最新价） | 广期所官网 | **official**，仅最新 |
| 碳酸锂（历史序列） | 东财 | relay |
| LME 铜锌镍 / 铁矿石 | 东财 → 新浪 | relay |

**关键设计：历史日行情文件是不可变的** —— 2026-10-08 收盘后那份文件的内容永不再变。
所以历史日期缓存 10 年（等于永久）、今天用短 TTL。这让 30 日走势的代价变成
「首次约 20 次请求，之后全免费」，缓存自然累积成本地历史库。

**为什么还需要转载源**：LME 官网 403、大商所 412，这两个没有可用的官方源。
东财本身也有限流（实测整个 IP 被限到 `code=000`），所以做了三层防护：
快速失败（8 秒超时）→ 磁盘缓存 → 新浪作第二源 → 陈旧缓存兜底（标注已过期）。

---

## 一个真实的数据完整性坑

接广期所官方源时发现：**它的接口忽略 `trade_date` 参数**。

```
请求 20260801 -> 主力 lc2701 收盘 117300
请求 20260901 -> 主力 lc2701 收盘 117300
请求 20260930 -> 主力 lc2701 收盘 117300
```

拿它拼历史序列会得到一条**首末相同的假水平线** —— 比没有数据更糟，因为它看起来像真的。
更严重的是 `get_price('碳酸锂', '2026-09-30')` 会把**今天的数据标成 9-30** 返回：
日期是错的、数字看着完全正常，这类错最难被发现。

对照实测上期所**真的按日期取历史**（20260825→107980、20260901→109220，主力按月换月 2610→2611），
确认差异后加了 `history: True/False` 标记（实测得出不是猜的），
`history=False` 的源只用于最新价，历史自动降级到转载源。

---

## 数据源权威性

同一句「沪铜 110,260 元/吨」，从上期所官网取的和从行情门户取到的，可信度不一样。
所以每个返回值都带 `source_tier`：

| 等级 | 含义 |
|---|---|
| `official` | 交易所 / 公司官网一手数据，可直接引用 |
| `relay` | 门户转载，数值通常一致但可能滞后 |
| `substitute` | 用相近但**不同**的东西顶替，**不等价** |

**实测拿不到的四个源**（原因记在 `cache.py` 的 `SOURCES` 里，不是漏了是评估过）：

| 源 | 情况 |
|---|---|
| LME 官网 | 403 地域封锁，官方行情需购买数据授权 |
| 上海钢联价格指数 | 指数接口 401 需签名认证与订阅；免费接口只给资讯不给指数值 |
| 大商所官网 | 日行情导出 412 反爬前置检查 |
| S&P Global | RSS 403 |

因此**铁矿石用大商所期货替代钢联现货指数**（标 `substitute`，口径不同），
**LME 走转载源**（标 `relay`）。

---

## 数据 schema

本地只用一个 SQLite 文件：`data/brief.sqlite3`。

```sql
CREATE TABLE articles (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,       -- 去重主键
    title        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',   -- 发布方
    author       TEXT,
    category     TEXT,
    published    TEXT,                       -- ISO8601，days 过滤靠它
    summary      TEXT,
    body         TEXT,                       -- 惰性抓取，未抓时为 NULL
    body_fetched TEXT,
    first_seen   TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'rss' -- 'rss'=可抓全文 / 'gnews'=仅标题摘要
);

-- 索引的是**归一化后**的文本（非 external-content，因为索引文本与原文不同）
CREATE VIRTUAL TABLE articles_fts USING fts5(
    search_text, tokenize='unicode61 remove_diacritics 2'
);
```

**去重**：`url` 唯一。RSS 每次抓取都重放最近几十条，靠这个唯一键幂等更新，不会重复入库。

**检索文本归一化**：`search_text` 是把标题+摘要+正文做 CJK 逐字空格化后的结果
（`Pilbara 锂矿` → `Pilbara 锂 矿`）。查询侧同样处理，再用短语查询 `"锂 矿"` 匹配，等价于子串匹配。

> 为什么不用 trigram 分词器（看似更适合中文）：它要求 token ≥3 字符，**「锂矿」这种两字词直接失效**。

**缓存**：HTTP 响应按 URL 的 SHA1 落 `data/cache/`，附一份 meta 记录抓取时间与 content-type。

---

## 已验证的事实

以下本机实测通过：

| 项 | 结果 |
|---|---|
| MCP 协议 | 3 个 server 经 stdio 连上、9 个工具可调 |
| LangGraph 两节点 | 消息流转正常：`system → human → ai(tool_calls) → tool(结果) → ai(最终回答)` |
| 工具转换 | 9 个 MCP 工具全部转成 LangChain `StructuredTool`，参数 schema 正确，实调成功 |
| 新闻语料 | 862 篇，2026-08-16 → 2026-10-08 |
| 中文检索 | 查「碳酸锂 价格」返回中文源真实报价新闻 |
| 储量 · JORC | Pilbara 年报 224 页扫出 Pilgangoora Indicated **354 Mt @ 1.28% Li₂O** |
| 储量 · NI 43-101 | Implats AIF 抽出的 6 个 Indicated/Inferred 数值与原文**逐字一致** |
| 价格 · 官方直连 | 沪铜 110,260 / 沪镍 118,750 / 沪锌 26,060 / 碳酸锂 117,300 |
| 价格 · 历史 | 沪铜 27 个交易日序列（官方）、碳酸锂 30 日 −22.91%（转载） |

**未验证**：真实模型下的对话效果。这一步需要 API 密钥。

---

## 已知局限

1. **`known_reports` 是人工白名单**，只登记了 Pilbara 与 Implats 两份报告，不是全网自动发现。
   要真正好用需要接交易所公告系统（SEDAR+ / ASX），是另一个量级的工程。
2. **广期所无历史** —— 碳酸锂的历史序列走转载源，权威性低于官方。
3. **铁矿石是期货不是现货指数**，与题面点名的上海钢联口径不同。
4. **对话历史会一直增长**，长对话最终会超出上下文窗口。目前没做裁剪。
5. **`mcp-config.json` 里是绝对路径**，换机器要手工改。

---

## 目录结构

```
mineral-daily-brief/v2/
├── cache.py            数据缓存层（三个 server 共同依赖）
│                       ├─ 数据源登记表（权威性等级）
│                       ├─ HTTP 取数：浏览器 UA、重试、磁盘缓存、陈旧兜底
│                       └─ SQLite + CJK 感知 FTS5 全文检索
├── news_server.py      mining-news MCP（FastMCP）+ 中英矿业术语词典
├── pdf_server.py       mineral-pdf MCP（FastMCP）+ 按坐标重建表格的解析器
├── price_server.py     lme-price MCP（FastMCP）+ 品种表 + 四个价格源
├── agent.py            LangGraph 两节点 + MCP 工具加载 + 命令行聊天
├── web.py              网页后端（复用 agent 的图 + 数据预览接口）
├── index.html          网页前端（一页，无构建步骤）
├── mcp-config.json     MCP 客户端配置（绝对路径）
├── Dockerfile / docker-compose.yml
├── pyproject.toml / .env.example / .gitignore / .dockerignore
├── RUN.md / README.md
└── data/               运行时生成：SQLite 库 + HTTP 缓存
```
