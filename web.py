"""矿权日报 · Web 界面 —— 左边 Agent 聊天，右边数据预览。

    python web.py            → http://127.0.0.1:8000

复用 agent.py 的图和工具加载，不重写一套。只依赖已经装好的 starlette + uvicorn
（fastmcp 带进来的），不引入新依赖。

四个接口：
    GET  /               页面
    GET  /api/status     就绪状态（模型、工具清单、缺密钥时的原因）
    POST /api/chat       Agent 对话，SSE 流式吐工具调用与回答
    GET  /api/overview   数据预览：新闻库统计 + 价格快照 + 数据源
    GET  /api/news       新闻列表（可搜索）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 静音 FastMCP 日志（必须在 import fastmcp 之前）
os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_ENABLE_RICH_LOGGING", "false")

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

import agent  # noqa: E402
import cache  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

# 绑哪个地址。默认只绑本机（安全）；容器里要设 HOST=0.0.0.0，否则从宿主机访问不到。
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))

# stdout 被重定向成管道时，Windows 上用的是 GBK。任何编不出来的字符（emoji 是最常见的）
# 都会抛 UnicodeEncodeError —— 而这是在启动横幅里，会直接把服务打死。
# 只改错误处理、不改编码：控制台该是什么编码还是什么编码，但永远不再因此崩。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 数据预览里展示的品种。挑主流的几个，全量 8 个要等太久。
PREVIEW_COMMODITIES = [
    "gfex_lithium_carbonate",
    "shfe_copper",
    "shfe_nickel",
    "shfe_zinc",
    "lme_copper",
    "dce_iron_ore",
]


def _text_of(result) -> str:
    """从 MCP 调用结果里取文本，用于工具结果的预览。"""
    data = getattr(result, "data", None)
    if data is not None:
        s = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    else:
        s = " ".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
    return s.replace("\n", " ")[:400]


async def _call(state, tool: str, args: dict):
    return await state.client.call_tool(tool, args)


# ---------------------------------------------------------------- 生命周期


async def _warmup(state):
    """本地库为空时后台先抓一轮，否则数据预览那一栏一片空白。

    调一次 search 就会触发 news_server 里的 _ensure_fresh 做全量 RSS 抓取
    （实测约 15 秒 / 400 多篇）。不阻塞启动 —— 页面先出来，数据随后填上。
    """
    conn = cache.connect()
    try:
        if cache.count_articles(conn):
            return
    finally:
        conn.close()
    try:
        print("  本地新闻库为空，后台抓取中（约 15 秒）…")
        await state.client.call_tool(
            "mining-news_search", {"query": "lithium", "days": 30, "limit": 1}
        )
        conn = cache.connect()
        print(f"  抓取完成，本地 {cache.count_articles(conn)} 条")
        conn.close()
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响其他功能
        print(f"  后台抓取失败：{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: Starlette):
    from fastmcp import Client

    client = Client(agent.mcp_config())
    await client.__aenter__()
    app.state.client = client
    app.state.tools = [agent.make_tool(client, t) for t in await client.list_tools()]
    app.state.tool_names = [t.name for t in await client.list_tools()]
    app.state.history = []
    app.state.lock = asyncio.Lock()

    # 没配密钥也要能起来 —— 否则数据预览就白做了。缺密钥时把原因记下来，
    # 页面上照实显示，聊天区禁用。
    try:
        app.state.graph = agent.build_graph(agent.make_llm(), app.state.tools)
        app.state.model = os.environ.get("MDB_MODEL") or "deepseek-chat"
        app.state.llm_error = None
    except SystemExit as exc:
        app.state.graph = None
        app.state.model = None
        app.state.llm_error = str(exc)

    bind = "  （容器内绑定 0.0.0.0，浏览器开 127.0.0.1）" if HOST == "0.0.0.0" else ""
    print(f"矿权日报 Web  → http://127.0.0.1:{PORT}{bind}")
    print(f"  工具 {len(app.state.tool_names)} 个 | 模型 {app.state.model or '未配置'}")
    if app.state.llm_error:
        print(f"  注意：{app.state.llm_error}")

    app.state.warmup = asyncio.create_task(_warmup(app.state))
    yield
    app.state.warmup.cancel()
    await client.__aexit__(None, None, None)


# ---------------------------------------------------------------- 页面


async def index(request):
    html = (REPO_ROOT / "index.html").read_text("utf-8")
    return HTMLResponse(html)


async def status(request):
    s = request.app.state
    return JSONResponse(
        {
            "ready": s.graph is not None,
            "model": s.model,
            "error": s.llm_error,
            "tools": s.tool_names,
        }
    )


# ---------------------------------------------------------------- 聊天


async def chat(request):
    """SSE 流：把 LangGraph 两个节点的产出实时推给前端。

    agent 节点产出 AIMessage（要么带 tool_calls，要么是最终回答），
    tools 节点产出 ToolMessage。据此转成四种事件。
    """
    from langchain_core.messages import HumanMessage

    s = request.app.state
    try:
        body = await request.json()
    except Exception:
        # 请求体不是合法 JSON（常见于客户端用 GBK 发中文），
        # 回一句人能看懂的 400，而不是一个没头没尾的 500。
        return JSONResponse(
            {"error": "请求体不是 UTF-8 编码的合法 JSON"}, status_code=400
        )
    question = (body.get("message") or "").strip()
    if not question:
        return JSONResponse({"error": "message 不能为空"}, status_code=400)

    async def stream():
        if s.graph is None:
            yield {"event": "error", "data": json.dumps({"message": s.llm_error}, ensure_ascii=False)}
            return

        async with s.lock:
            s.history.append(HumanMessage(question))
            try:
                async for chunk in s.graph.astream({"messages": s.history}, stream_mode="updates"):
                    for node, update in chunk.items():
                        for m in update.get("messages", []):
                            if node == "agent":
                                for tc in getattr(m, "tool_calls", None) or []:
                                    yield {
                                        "event": "tool_call",
                                        "data": json.dumps(
                                            {
                                                "name": tc["name"],
                                                "args": json.dumps(tc["args"], ensure_ascii=False)[:200],
                                            },
                                            ensure_ascii=False,
                                        ),
                                    }
                                if not getattr(m, "tool_calls", None) and m.content:
                                    yield {
                                        "event": "answer",
                                        "data": json.dumps({"content": m.content}, ensure_ascii=False),
                                    }
                            elif node == "tools":
                                yield {
                                    "event": "tool_result",
                                    "data": json.dumps(
                                        {"name": getattr(m, "name", ""), "preview": str(m.content)},
                                        ensure_ascii=False,
                                    ),
                                }
                    # 让出控制权，事件才会即时到达浏览器
                    await asyncio.sleep(0)
                yield {"event": "done", "data": "{}"}
            except Exception as exc:  # noqa: BLE001 - 任何异常都如实告诉前端
                yield {
                    "event": "error",
                    "data": json.dumps({"message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
                }

    return EventSourceResponse(stream())


async def reset(request):
    request.app.state.history = []
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- 数据预览


async def overview(request):
    """新闻库统计 + 各品种价格快照 + 数据源分级。价格并发取，约 2-4 秒。"""
    s = request.app.state

    conn = cache.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(body IS NOT NULL) with_body, "
            "MIN(published) lo, MAX(published) hi FROM articles"
        ).fetchone()
        stats = {
            "articles": row["n"],
            "with_body": row["with_body"] or 0,
            "earliest": (row["lo"] or "")[:10],
            "latest": (row["hi"] or "")[:10],
        }
    finally:
        conn.close()

    async def one(key):
        try:
            r = await s.client.call_tool("lme-price_get_price", {"commodity": key})
            d = r.data or {}
            return {
                "key": key,
                "name": d.get("name_zh"),
                "price": d.get("price"),
                "unit": d.get("unit"),
                "change_pct": d.get("change_pct"),
                "date": d.get("date"),
                "tier": d.get("source_tier"),
                "source": d.get("source"),
                "error": d.get("error"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"key": key, "error": str(exc)[:80]}

    prices = await asyncio.gather(*(one(k) for k in PREVIEW_COMMODITIES))

    return JSONResponse(
        {
            "stats": stats,
            "prices": prices,
            "sources": {
                "tiers": cache.TIER_LABEL,
                "available": [
                    {"name": v["name"], "tier": v["tier"], "note": v["note"]}
                    for v in cache.SOURCES.values()
                    if v.get("available", True)
                ],
                "unavailable": cache.unavailable_sources(),
            },
        }
    )


async def news(request):
    """新闻列表。带 q 就用 FTS5 检索，否则按时间倒序。"""
    q = (request.query_params.get("q") or "").strip()
    limit = min(int(request.query_params.get("limit") or 25), 100)

    conn = cache.connect()
    try:
        if q:
            rows = cache.search_articles(conn, q, limit=limit)
        else:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,url,title,source,published,summary,origin,"
                    "(body IS NOT NULL) AS has_body FROM articles "
                    "ORDER BY published DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
    finally:
        conn.close()

    return JSONResponse(
        {
            "query": q,
            "count": len(rows),
            "items": [
                {
                    "title": r["title"],
                    "url": r["url"],
                    "source": r["source"],
                    "published": (r["published"] or "")[:10],
                    "full_text": r["origin"] == "rss",
                    "has_body": bool(r.get("has_body")),
                }
                for r in rows
            ],
        }
    )


async def trend(request):
    """单个品种的走势序列，给前端画 sparkline。"""
    key = request.query_params.get("commodity") or "gfex_lithium_carbonate"
    r = await request.app.state.client.call_tool(
        "lme-price_get_trend", {"commodity": key, "days": 30}
    )
    d = r.data or {}
    return JSONResponse(
        {
            "name": d.get("name_zh"),
            "unit": d.get("unit"),
            "change_pct": d.get("change_pct"),
            "tier": d.get("source_tier"),
            "series": [x["close"] for x in (d.get("series") or []) if x.get("close") is not None],
            "dates": [x["date"] for x in (d.get("series") or [])],
        }
    )


from sse_starlette.sse import EventSourceResponse  # noqa: E402

app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/status", status),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/reset", reset, methods=["POST"]),
        Route("/api/overview", overview),
        Route("/api/news", news),
        Route("/api/trend", trend),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
