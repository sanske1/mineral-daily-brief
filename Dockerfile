# 一个镜像装下 3 个 MCP server + Agent。
#
# 源码都在 /app/src 下；数据目录是 /app/data（声明成卷）。
# 默认命令起网页；3 个 MCP server 由 agent.py 自己作为子进程拉起，也可以单独跑，见 RUN.md。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    MDB_DATA_DIR=/app/data \
    FASTMCP_LOG_ENABLED=false \
    FASTMCP_SHOW_SERVER_BANNER=false

WORKDIR /app

# 依赖单独一层：改代码不会触发重装。
#
# 注意这里**不能**接 `&& find ... || true` 之类的清理 —— `A && B || true` 里
# A 失败时 true 照样执行，pip 装不上也会「构建成功」，产出坏镜像骗自己。
# 清理那点收益不值得冒这个险。
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastmcp>=4.0" "langgraph>=1.0" "langchain-openai>=1.0" \
      "httpx>=0.27" "pymupdf>=1.24" "pdfplumber>=0.11" "python-dotenv>=1.0"

# 代码 + 页面：一个缓存层 + 三个 server + 一个 agent + 一个网页，都在 src/ 下
COPY src/ ./src/

# 运行时状态（SQLite 库 + HTTP 缓存）都在这里。声明成卷，容器删了数据不丢。
RUN mkdir -p /app/data

# 预置新闻库（676 篇，2026-08-17 ~ 10-08）烤进镜像。
# 容器首次挂一个空数据卷时，Docker 会把镜像里 /app/data 的内容拷进卷里 ——
# 所以 `docker run` 一启动就有新闻可搜，不用等那一轮后台抓取。
# 卷已经存在的话不会被覆盖（只在首次创建时播种）。
COPY data/brief.sqlite3 /app/data/brief.sqlite3

VOLUME ["/app/data"]

# 容器里必须绑 0.0.0.0，否则从宿主机访问不到（web.py 默认只绑 127.0.0.1）
ENV HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

# 默认起网页。要命令行聊天或跑某个 MCP server，覆盖 command：
#   docker run -it --rm <镜像> python src/agent.py          # 命令行聊天
#   docker run -i --rm <镜像> python src/news_server.py     # 当 MCP server 用
CMD ["python", "src/web.py"]
