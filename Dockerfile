# 一个镜像装下 3 个 MCP server + Agent。
#
# 默认命令是 Agent 的交互聊天；3 个 MCP server 由 agent.py 自己作为子进程拉起
# （它们都在同一个镜像里），也可以单独跑，见 RUN.md。
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

# 代码 + 页面：一个缓存层 + 三个 server + 一个 agent + 一个网页
COPY cache.py news_server.py pdf_server.py price_server.py agent.py web.py index.html ./

# 运行时状态（SQLite 库 + HTTP 缓存）都在这里。声明成卷，容器删了数据不丢。
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 容器里必须绑 0.0.0.0，否则从宿主机访问不到（web.py 默认只绑 127.0.0.1）
ENV HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

# 默认起网页。要命令行聊天或跑某个 MCP server，覆盖 command：
#   docker run -it --rm <镜像> python agent.py          # 命令行聊天
#   docker run -i --rm <镜像> python news_server.py     # 当 MCP server 用
CMD ["python", "web.py"]
