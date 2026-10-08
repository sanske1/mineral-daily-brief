# 矿权日报 Agent — 运行说明

3 个 MCP server（FastMCP）+ 1 个 LangGraph 两节点 Agent + 1 个网页。

**6 个 Python 文件 + 1 个页面**：

| 文件 | 作用 |
|---|---|
| `cache.py` | 数据缓存层：HTTP 取数（浏览器 UA / 重试 / 磁盘缓存）+ SQLite 存储 + 数据源登记 |
| `news_server.py` | mining-news MCP：新闻检索 + 正文抓取 |
| `pdf_server.py` | mineral-pdf MCP：NI 43-101 / JORC 储量表抽取 |
| `price_server.py` | lme-price MCP：上期所 / 广期所 / LME 行情 |
| `agent.py` | LangGraph 两节点编排（命令行聊天入口） |
| `web.py` | 网页后端：复用 agent 的图，加数据预览接口 |
| `index.html` | 网页前端：左边对话、右边数据 |

---

## 5 分钟跑起来

### 1. 装依赖（约 90 秒）

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -r pyproject.toml
```

> macOS / Linux 把 `.venv/Scripts/python.exe` 换成 `.venv/bin/python`。以下同。

### 2. 打开网页

```bash
./.venv/Scripts/python.exe web.py
```

浏览器开 **http://127.0.0.1:8000**。左边是 Agent 对话，右边是数据预览
（新闻库 / 价格 / 数据源三个标签页）。

**API Key 直接在网页上填**，页面会有一个输入框。不用建 `.env`，不用重启。
填过的密钥存在 `data/web_config.json`（只在你本机），下次启动不用重填。

> 首次启动本地新闻库是空的，后台会自动抓一轮（约 15 秒），新闻标签页会自己刷新出来。
> 这一步不需要密钥 —— 右边的数据预览在没有 Key 时也能用。

**也可以走 .env**（命令行模式、CI、或想固定配置时用）：

```bash
cp .env.example .env      # 填 OPENAI_API_KEY / OPENAI_BASE_URL / MDB_MODEL
```

优先级：**网页里填的 > 环境变量**。

### 3b. 或者用命令行

```bash
./.venv/Scripts/python.exe agent.py
```

```
矿权日报 Agent（LangGraph 两节点）· 模型 deepseek-chat @ https://api.deepseek.com
已从 3 个 MCP server 加载 9 个工具

你 > Pilbara 锂矿的储量和最近锂价怎么样
  → mining-news_search({"query": "Pilbara 锂矿", "days": 30})
    ✓ {"query": "Pilbara 锂矿", ...
  → mineral-pdf_known_reports({"company": "Pilbara"})
    ✓ {"count": 1, "reports": [...
  → mineral-pdf_extract_resources({"pdf_url": "https://www.pls.com/...
    ✓ {"source": "https://www.pls.com/...
  → lme-price_get_trend({"commodity": "碳酸锂", "days": 30})
    ✓ {"commodity": "gfex_lithium_carbonate", ...

碳酸锂收 117,300 元/吨（广期所官网，10 月 8 日，当日 −1.28%），
近 30 个交易日 −22.91%。Pilgangoora 资源量：Measured 16 Mt @ 1.27%、
Indicated 354 Mt @ 1.28%、Inferred 70 Mt @ 1.25% Li₂O（2026 年报第 34 页，JORC 口径）。
...
```

`/exit` 退出。每次工具调用和返回都会打印出来 —— 你能当场判断每个数字是怎么来的。

---

## 图长什么样

```
START ──► agent ──(有 tool_calls)──► tools ──┐
            │                                │
            └──(没有)──► END                 │
            ▲                                │
            └────────────────────────────────┘
```

- **agent 节点**：把消息历史交给绑定了工具的模型
- **tools 节点**：LangGraph 的 `ToolNode`，执行模型要调的工具
- 工具在启动时从 3 个 MCP server 一次性加载，包成 LangChain 工具

代码在 `agent.py` 的 `build_graph()`，二十来行。

---

## Docker 方式

### 从 GitHub 拉现成镜像跑（不用装 Python，也不用配密钥）

推代码到 GitHub 后，`.github/workflows/docker-publish.yml` 会自动把镜像推到 GHCR。
之后任何装了 Docker 的机器，**一条命令**：

```bash
docker run -d --name mdb -p 8000:8000 ghcr.io/<你的用户名>/<仓库名>:latest
```

浏览器开 **http://localhost:8000**，页面上会有一个 **API Key 输入框** —— 填进去点「开始使用」
就能对话。不用 `.env`，不用给 `docker run` 传任何参数。

密钥存在容器内 `/app/data/web_config.json`（**只在你本机**，不上传，也不在镜像里）。
想让它在容器删掉后还在，加一个卷：

```bash
docker run -d --name mdb -p 8000:8000 -v mdb-data:/app/data ghcr.io/<你的用户名>/<仓库名>:latest
```

这么一来容器重建也不用重填密钥，新闻库缓存也一起保留。

> 私有仓库的镜像包默认也是私有的，要先登录：
> ```bash
> echo $GITHUB_TOKEN | docker login ghcr.io -u <你的用户名> --password-stdin
> ```
> 想公开：GitHub 仓库页 → Packages → 该镜像 → Package settings → Change visibility。

**想用命令行聊天**（而不是网页），覆盖掉默认命令：

```bash
docker run -it --rm -v mdb-data:/app/data \
  -e OPENAI_API_KEY=sk-你的密钥 \
  ghcr.io/<你的用户名>/<仓库名>:latest python agent.py
```

三个 MCP server 也可以单独起：

```bash
docker run -i --rm ghcr.io/<你的用户名>/<仓库名>:latest python news_server.py
```

### 用 docker compose

```bash
echo "MDB_IMAGE=ghcr.io/<你的用户名>/<仓库名>:latest" > .env.docker
docker compose --env-file .env.docker up -d web         # 网页，密钥在页面上填
docker compose --env-file .env.docker run --rm agent    # 命令行
```

`MDB_IMAGE` 不设的话，compose 会退回用本地构建的 `mineral-daily-brief:latest`。

### 本地构建（改了代码想测）

```bash
docker compose up -d --build web
```

首次构建约 3–5 分钟（pymupdf / langgraph 的轮子较大），之后走缓存。镜像约 607 MB。

### 把 server 当容器跑给别的 MCP 客户端用

```bash
docker compose up -d mining-news mineral-pdf lme-price
```

日常聊天**不需要**这一步 —— `agent` 服务会自己把这 3 个 server 当子进程拉起来。

---

## 推送到 GitHub（一次性设置）

镜像推不上去，是 GitHub 那边还没配。步骤：

```bash
cd "C:/Users/hu/Desktop/矿物面试题/mineral-daily-brief/v2"
```

```bash
git init -b main
```

```bash
git add -A
```

```bash
git commit -m "矿权日报 Agent"
```

```bash
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
```

```bash
git push -u origin main
```

推上去之后去仓库的 **Actions** 页面，会看到「构建并推送镜像到 GHCR」在跑。
跑完（约 3–5 分钟）在仓库首页右侧 **Packages** 里就能看到镜像，
地址就是 `ghcr.io/<你的用户名>/<仓库名>:latest`。

> `.gitignore` 已经把 `.env`、`.venv/`、`data/`、`mcp-config.json` 排除了，
> 不会把密钥和几百 MB 的缓存推上去。`git status` 里如果看到 `.env` 请停下来检查。

推 `v*` 标签会额外打一个版本号标签：

```bash
git tag v0.2.0 && git push origin v0.2.0
```

---

## 接到 Claude Desktop / Cursor

`mcp-config.json` 已经生成好了，内容是：

| server | 脚本 |
|---|---|
| `mining-news` | `news_server.py` |
| `mineral-pdf` | `pdf_server.py` |
| `lme-price` | `price_server.py` |

把它里面的 `mcpServers` **合并**进客户端配置（不是覆盖整个文件）：

- Windows：`%APPDATA%\Claude\claude_desktop_config.json`
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Cursor：`~/.cursor/mcp.json`

然后**完全退出并重开**客户端。

> ⚠️ `mcp-config.json` 里的路径是**绝对路径**。换机器或挪目录后要手工改这三处
> （`command` 指向装好依赖的解释器，`args` 指向三个 server 脚本）。

---

## 换模型供应商

代码用的是 `langchain_openai.ChatOpenAI`，改 `.env` 三个变量即可：

| 供应商 | `OPENAI_BASE_URL` | `MDB_MODEL` |
|---|---|---|
| DeepSeek（默认） | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

---

## 三个 server 能单独用吗？

能。这是刻意设计的 —— 每个 server 都不调用 LLM、不依赖外部服务（没有向量库、没有嵌入模型），
唯一的运行时状态是一个 SQLite 文件加一个 HTTP 缓存目录。可以只挂 `lme-price` 一个用，
另外两个不启动也不影响。

---

## 数据源与权威性

每个价格/新闻的返回体里都带 `source_tier`：

| 等级 | 含义 |
|---|---|
| `official` | 交易所 / 公司官网一手数据，可直接引用 |
| `relay` | 门户转载的官方行情，数值通常一致但可能滞后 |
| `substitute` | 用相近但**不同**的东西顶替，**不等价** |

**优先用官方源**：上期所（沪铜/沪锌/沪镍）与广期所（碳酸锂）的日行情公开免费，
所以这两个交易所的品种走官方直连，不走门户转载。

**四个源实测拿不到**（原因记在 `cache.py` 的 `SOURCES` 里）：

| 源 | 情况 |
|---|---|
| LME 官网 | 403 地域封锁，官方行情需购买授权 → LME 只能走转载 |
| 上海钢联价格指数 | 指数接口 401 需签名认证与订阅；免费接口只给资讯不给指数值 |
| 大商所官网 | 日行情导出 412 反爬前置检查 |
| S&P Global | RSS 403 |

因此**铁矿石用期货替代钢联现货指数**（标 `substitute`），**LME 走转载源**（标 `relay`）。

---

## 踩过的坑

| 现象 | 真实原因 | 处理 |
|---|---|---|
| `curl https://www.mining.com/feed/` 返回 403 | **不是反爬墙，是 UA 白名单**。加浏览器 UA 立刻 200 | `cache.py` 统一带 Chrome UA |
| **广期所官方接口取历史日期返回同样数据** | 它**忽略 `trade_date`**，永远返回最新 | 标记 `history: False`，该源只用于最新价 |
| 上期所数据里「主力合约」价格为空 | 每组末尾有汇总行，`DELIVERYMONTH` 是「小计」、收盘价为空，却因持仓量最大被选中 | 用 `\d{4}` 正则滤掉非合约月份的行 |
| 中文查询「锂矿」在英文语料上 0 命中 | FTS5 默认分词器按非字母数字切词，且语料是英文 | CJK 逐字空格化 + 中英术语扩展 + 按查询抓 Google News 中文源 |
| Google News 的链接抓不到正文 | 它**不会**重定向到原文，跟随重定向停在自家 JS 页 | `fetch_article` 明确返回 `full_text_unavailable`，**不拿 Google 页面冒充正文** |
| 东财接口间歇性限流 | 上游会限流（实测整个 IP 被限到 `code=000`） | 快速失败 + 磁盘缓存 + 新浪作第二源 |
| Windows 上临时 PDF 清理报 `WinError 32` | `tempfile.mkstemp` 返回的 fd 没关，文件被锁 | 立刻 `os.close(fd)` |
| FastMCP 启动日志很吵 | 默认往 stderr 打 rich 日志，不污染协议但糊终端 | `FASTMCP_LOG_ENABLED=false` 等三个环境变量关掉 |
| `langchain-mcp-adapters` 装不上 | 它要求 `mcp<2.0.0`，而 fastmcp 4 要求 `mcp>=2.0.0`，**冲突** | 工具转换手写（`agent.py` 的 `make_tool()`，20 行） |

---

## 目录结构

```
mineral-daily-brief/v2/
├── cache.py            数据缓存层（三个 server 共同依赖）
├── news_server.py      mining-news MCP
├── pdf_server.py       mineral-pdf MCP
├── price_server.py     lme-price MCP
├── agent.py            LangGraph 两节点 + 命令行聊天入口
├── web.py              网页后端（复用 agent 的图 + 数据预览接口）
├── index.html          网页前端（一页，无构建步骤）
├── mcp-config.json     MCP 客户端配置（绝对路径，换机器要改）
├── Dockerfile / docker-compose.yml
├── .github/workflows/docker-publish.yml   推到 main 自动构建并推 GHCR
├── pyproject.toml / .env.example / .gitignore / .dockerignore
├── RUN.md / README.md
└── data/               运行时生成：SQLite 库 + HTTP 缓存
```

网页只用 starlette + uvicorn（fastmcp 已经带进来的），**没有引入新依赖**，
也没有前端构建步骤 —— `index.html` 直接打开就能改。
