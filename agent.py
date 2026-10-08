"""矿权日报 Agent —— LangGraph 两节点。

    python agent.py            交互聊天

图长这样：

    START ──► agent ──(有 tool_calls)──► tools ──┐
                │                                │
                └──(没有)──► END                 │
                ▲                                │
                └────────────────────────────────┘

工具来自 3 个 MCP server（mining-news / mineral-pdf / lme-price），
启动时一次性加载，包成 LangChain 工具交给 ToolNode。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# FastMCP 的启动日志很吵（走 stderr，不污染协议，但会糊住终端）。必须在 import 之前关。
os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_ENABLE_RICH_LOGGING", "false")

from fastmcp import Client  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from pydantic import create_model  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

# MCP server 注册表
SERVERS = {
    "mining-news": "news_server.py",
    "mineral-pdf": "pdf_server.py",
    "lme-price": "price_server.py",
}

SYSTEM = """你是「矿权日报」分析助手。你可以检索矿业新闻、抓取正文、从 NI 43-101 / JORC
报告 PDF 里抽取储量表、查询上期所/广期所/LME 的价格与走势。

回答要求：
- 直接回答用户问的事，需要时才出完整简报。
- 每个数字都要来自工具返回，不要凭记忆或心算。价格要标数据源。
- 工具说拿不到，就说拿不到（比如全文不可用、没识别出储量表），不要用别的数字顶替。
- 推算值（字段名带 derived）要说明是推算的。
- 检索不理想时换个说法重试（实测「项目名」比「公司名」更容易命中行业报道）。
- 用中文回答。
"""


def mcp_config() -> dict:
    """3 个 MCP server 的启动配置。用当前解释器，否则子进程 import 不到 fastmcp。"""
    return {
        "mcpServers": {
            name: {
                "command": sys.executable,
                "args": [str((REPO_ROOT / script).resolve())],
                "env": {
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",  # Windows 上不给会因 GBK 编码把管道搞坏
                    "MDB_DATA_DIR": str((REPO_ROOT / "data").resolve()),
                    "FASTMCP_LOG_ENABLED": "false",
                    "FASTMCP_SHOW_SERVER_BANNER": "false",
                },
            }
            for name, script in SERVERS.items()
        }
    }


def _args_schema(tool) -> type:
    """从 MCP 工具的 input_schema 生成 pydantic 模型，供 StructuredTool 用。"""
    schema = getattr(tool, "input_schema", None) or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    types = {"integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
    fields = {
        k: (types.get(v.get("type", "string"), str), ... if k in required else v.get("default"))
        for k, v in props.items()
    }
    return create_model(f"{tool.name.replace('-', '_')}_args", **fields)


def make_tool(client: Client, tool) -> StructuredTool:
    """把一个 MCP 工具包成 LangChain 工具。

    闭包持有 client —— 所以 client 必须活到整场对话结束，工具调用时连接还在。
    """

    async def call(**kwargs):
        result = await client.call_tool(tool.name, kwargs)
        data = getattr(result, "data", None)
        if data is not None:
            return data if isinstance(data, (dict, list)) else str(data)
        return str(result.content)

    return StructuredTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=_args_schema(tool),
        coroutine=call,
    )


def build_graph(llm: ChatOpenAI, tools: list[StructuredTool]):
    """两个节点：agent（调模型）+ tools（ToolNode 执行工具）。"""
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: MessagesState):
        return {"messages": [await llm_with_tools.ainvoke(state["messages"])]}

    def route(state: MessagesState):
        """模型这一轮调工具了就去 tools，否则收尾。"""
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile()


def make_llm() -> ChatOpenAI:
    key = os.environ.get("OPENAI_API_KEY") or ""
    if not key or key.startswith("sk-在这里"):
        raise SystemExit(
            "没有 API 密钥。先 `cp .env.example .env`，把 OPENAI_API_KEY 填成真实密钥。"
        )
    return ChatOpenAI(
        model=os.environ.get("MDB_MODEL") or "deepseek-chat",
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com",
        temperature=0.2,
    )


async def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    llm = make_llm()
    print(f"矿权日报 Agent（LangGraph 两节点）· 模型 {llm.model_name} @ {llm.openai_api_base}")
    print("直接提问，/exit 退出\n")

    async with Client(mcp_config()) as client:
        tools = [make_tool(client, t) for t in await client.list_tools()]
        app = build_graph(llm, tools)
        print(f"已从 {len(SERVERS)} 个 MCP server 加载 {len(tools)} 个工具\n")

        messages: list = [SystemMessage(SYSTEM)]
        while True:
            try:
                question = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if question in ("/exit", "/quit", "/q"):
                break
            if not question:
                continue

            messages.append(HumanMessage(question))
            state = await app.ainvoke({"messages": messages})

            # 打印这一轮新产生的消息：工具调用 + 最终回答
            for m in state["messages"][len(messages):]:
                for tc in getattr(m, "tool_calls", None) or []:
                    args = json.dumps(tc["args"], ensure_ascii=False)
                    print(f"  → {tc['name']}({args[:100]})")
                if m.type == "tool":
                    body = str(m.content).replace("\n", " ")
                    print(f"    ✓ {body[:120]}")

            messages = state["messages"]
            print("\n" + (state["messages"][-1].content or "（无内容）") + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
