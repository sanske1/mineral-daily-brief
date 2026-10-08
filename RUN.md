# 矿权日报 Agent — 运行说明

3 个 MCP server（FastMCP）+ 1 个 LangGraph 三节点 Agent + 1 个网页。

两种用法，按你手上有什么选：

| 方式 | 需要什么 | 结果 |
|---|---|---|
| **一、Docker** | 装了 Docker，别的都不用 | 拉镜像 → 起容器 → 打开网页 |
| **二、本地 venv** | Python 3.11 + uv | 建 venv → 起服务 → 打开网页 |

两条路都**不需要 `.env`** —— API Key 在网页上填。

---

## 一、Docker：拉下来就能用

### 1. 拉镜像

```bash
docker pull ghcr.io/sanske1/mineral-daily-brief:latest
```

约 608 MB。镜像里已经装好全部依赖，也**预置了一份新闻库**（676 篇，2026-08-17 ~ 10-08），
所以不需要本地装 Python、不需要联网抓新闻。

### 2. 起容器

```bash
docker run -d --name mineral-web -p 8000:8000 ghcr.io/sanske1/mineral-daily-brief:latest
```

### 3. 打开网页

浏览器开 **http://localhost:8000**（容器起来约 8 秒就绪）。

页面左侧是 Agent 对话，右侧是数据预览（**新闻库 / 价格 / 数据源**三个标签页）。
第一次用会看到一个 **API Key 输入框** —— 填进去点「开始使用」，就能对话了。

> 页面上填的密钥存在容器的 `/app/data/web_config.json`，**只在你本机**，不上传、也不在镜像里。
> 容器重建就要重填 —— 想留住就加个数据卷，见下。

### 换端口

```bash
docker run -d --name mineral-web -p 9000:8000 ghcr.io/sanske1/mineral-daily-brief:latest
```

左边是宿主机端口，右边 `8000` 是容器内固定的。上面这条 → 开 **http://localhost:9000**。

### 让密钥和缓存留在容器外（推荐）

```bash
docker run -d --name mineral-web -p 8000:8000 -v mdb-data:/app/data ghcr.io/sanske1/mineral-daily-brief:latest
```

加上 `-v` 之后，密钥、新闻库、价格缓存、简报存档都在具名卷 `mdb-data` 里 ——
容器删了重建也不用重填密钥。

> 卷**第一次创建**时，Docker 会把镜像里 `/app/data` 的内容拷进卷里，
> 所以预置的 676 篇新闻照样在。卷已经存在的话不会被覆盖。

### 用命令行聊天（而不是网页）

镜像默认跑网页，覆盖掉命令即可：

```bash
docker run -it --rm -v mdb-data:/app/data \
  -e OPENAI_API_KEY=sk-你的密钥 \
  ghcr.io/sanske1/mineral-daily-brief:latest python agent.py
```

### 三个 MCP server 单独起

给别的 MCP 客户端当数据源用：

```bash
docker run -i --rm ghcr.io/sanske1/mineral-daily-brief:latest python news_server.py
```

日常聊天**不需要**这一步 —— Agent 会自己把这 3 个 server 当子进程拉起来。

### 或者：克隆源码本地构建

不想用现成镜像（改了代码、或者想离线）：

```bash
git clone https://github.com/sanske1/mineral-daily-brief.git
cd mineral-daily-brief
```

```bash
docker compose up -d web
```

首次构建约 3–5 分钟（`pymupdf` / `langgraph` 的轮子较大），之后走缓存。
跑的仍是同一个 `Dockerfile`，效果和拉镜像一样。

> 推代码到 `main` 分支时，`.github/workflows/docker-publish.yml` 会自动重新构建并推 GHCR，
> 所以改了代码只要 push，别人 `docker pull` 就是新版。

---

## 二、本地 venv：起网页 / 跑命令行

### 1. 建 venv 装依赖（约 90 秒）

```bash
uv venv .venv --python 3.11
```

```bash
uv pip install --python .venv/Scripts/python.exe -r pyproject.toml
```

> macOS / Linux 把 `.venv/Scripts/python.exe` 换成 `.venv/bin/python`。以下同。

### 2. 起网页

```bash
./.venv/Scripts/python.exe web.py
```

浏览器开 **http://127.0.0.1:8000** —— 和 Docker 那个网页完全一样，左侧对话、右侧数据预览。

API Key 同样在页面上填，不用建 `.env`。填过的存在 `data/web_config.json`，下次启动不用重填。

端口被占就换一个：

```bash
PORT=9000 ./.venv/Scripts/python.exe web.py
```

> `web.py` 默认只绑 `127.0.0.1`（本机），要给别人访问才需要 `HOST=0.0.0.0`。
> 容器里就是靠这个变量绑 `0.0.0.0` 的。

### 3. 或者跑命令行聊天

`agent.py` 是**命令行入口**，不起网页：

```bash
./.venv/Scripts/python.exe agent.py
```

```
矿权日报 Agent · 模型 deepseek-chat @ https://api.deepseek.com
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
```

`/exit` 退出。每次工具调用和返回都打印出来，能当场看清每个数字是怎么来的。

命令行模式**必须**有密钥，走 `.env`：

```bash
cp .env.example .env      # 填 OPENAI_API_KEY / OPENAI_BASE_URL / MDB_MODEL
```

### 关于密钥的优先级

**网页里填的 > 环境变量**。所以旧 `.env` 不会盖掉你在网页上填的。

### 关于 `data/` 目录

```
data/
├── brief.sqlite3     新闻库（仓库里预置了 676 篇，删掉会自动重新抓）
├── web_config.json   网页上填的 API Key
├── briefs/           生成过的简报存档（Markdown）
└── cache/            HTTP 磁盘缓存（行情历史 + RSS 原文）
```

`brief.sqlite3` 随仓库分发、也烤进了镜像；其余三个是运行时产生的，不入库。

---

## 三、接到 Claude Desktop / Cursor

三个 server 都可以单独挂进 MCP 客户端。在客户端配置里加上这一段
（**合并**进已有的 `mcpServers`，不是覆盖整个文件）：

```json
{
  "mcpServers": {
    "mining-news": {
      "command": "<仓库路径>/.venv/Scripts/python.exe",
      "args": ["<仓库路径>/news_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "MDB_DATA_DIR": "<仓库路径>/data" }
    },
    "mineral-pdf": {
      "command": "<仓库路径>/.venv/Scripts/python.exe",
      "args": ["<仓库路径>/pdf_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "MDB_DATA_DIR": "<仓库路径>/data" }
    },
    "lme-price": {
      "command": "<仓库路径>/.venv/Scripts/python.exe",
      "args": ["<仓库路径>/price_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "MDB_DATA_DIR": "<仓库路径>/data" }
    }
  }
}
```

把 `<仓库路径>` 换成仓库的绝对路径，macOS / Linux 还要把
`.venv/Scripts/python.exe` 换成 `.venv/bin/python`。

客户端配置文件的位置：

- Windows：`%APPDATA%\Claude\claude_desktop_config.json`
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Cursor：`~/.cursor/mcp.json`

改完**完全退出并重开**客户端。

> 这三个 server 不调用 LLM、不依赖外部服务，所以可以只挂其中一个用，
> 另外两个不启动也不影响。

---

## 四、换模型供应商

代码用的是 `langchain_openai.ChatOpenAI`，改 `.env` 三个变量即可：

| 供应商 | `OPENAI_BASE_URL` | `MDB_MODEL` |
|---|---|---|
| DeepSeek（默认） | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

网页上填的密钥也走同一套 OpenAI 兼容协议，换供应商只要把 `base_url` 和模型名填对。

---

## 五、目录结构

```
mineral-daily-brief/
├── cache.py            数据缓存层（三个 server 共同依赖）
├── news_server.py      mining-news MCP
├── pdf_server.py       mineral-pdf MCP
├── price_server.py     lme-price MCP
├── agent.py            LangGraph 三节点 + MCP 工具加载 + 命令行入口
├── web.py              网页后端（复用 agent 的图 + 数据预览接口）
├── index.html          网页前端（一页，无构建步骤）
├── Dockerfile / docker-compose.yml
├── .github/workflows/docker-publish.yml   推到 main 自动构建并推 GHCR
├── pyproject.toml / .env.example / .gitignore / .dockerignore
├── README.md / RUN.md
└── data/
    ├── brief.sqlite3   预置新闻库（随仓库分发、也打进镜像）
    └── ...             运行时生成：密钥 / 简报存档 / HTTP 缓存
```

网页只用 starlette + uvicorn（fastmcp 已经带进来的），**没有引入新依赖**，
也没有前端构建步骤 —— `index.html` 直接打开就能改。

---

## 六、排错

| 现象 | 真实原因 | 处理 |
|---|---|---|
| `curl https://www.mining.com/feed/` 返回 403 | **不是反爬墙，是 UA 白名单**。加浏览器 UA 立刻 200 | `cache.py` 统一带 Chrome UA |
| **广期所官方接口取历史日期返回同样数据** | 它**忽略 `trade_date`**，永远返回最新 | 标记 `history: False`，该源只用于最新价 |
| 上期所数据里「主力合约」价格为空 | 每组末尾有汇总行，`DELIVERYMONTH` 是「小计」、收盘价为空，却因持仓量最大被选中 | 用 `\d{4}` 正则滤掉非合约月份的行 |
| 中文查询「锂矿」在英文语料上 0 命中 | FTS5 默认分词器按非字母数字切词，且语料是英文 | CJK 逐字空格化 + 中英术语扩展 + 按查询抓 Google News 中文源 |
| Google News 的链接抓不到正文 | 它**不会**重定向到原文，跟随重定向停在自家 JS 页 | `fetch_article` 明确返回 `full_text_unavailable`，**不拿 Google 页面冒充正文** |
| 东财接口间歇性限流 | 上游会限流（实测整个 IP 被限到 `code=000`） | 快速失败 + 磁盘缓存 + 新浪作第二源 |
| 网页上发出去没反应、控制台也没报错 | `sse-starlette` 的行尾是 `\r\n`，前端按 `\n\n` 切分解析不出事件 | 先对整个缓冲区做 `\r\n`→`\n` 归一化 |
| 所有可选参数被当成字符串 | `Annotated[X \| None]` 生成的是 `anyOf`、**没有顶层 `type`** | `_json_type_of()` 专门解析 `anyOf` |
| Windows 上临时 PDF 清理报 `WinError 32` | `tempfile.mkstemp` 返回的 fd 没关，文件被锁 | 立刻 `os.close(fd)` |
| FastMCP 启动日志很吵 | 默认往 stderr 打 rich 日志，不污染协议但糊终端 | `FASTMCP_LOG_ENABLED=false` 等三个环境变量关掉 |
| `langchain-mcp-adapters` 装不上 | 它要求 `mcp<2.0.0`，而 fastmcp 4 要求 `mcp>=2.0.0`，**冲突** | 工具转换手写（`agent.py` 的 `make_tool()`，20 行） |

**容器起不来时**：

```bash
docker logs mineral-web
```

```bash
curl -s http://127.0.0.1:8000/api/status
```

`/api/status` 会回 `ready`（密钥是否可用）、`model`、已加载的工具数 —— 一眼看出卡在哪一步。
