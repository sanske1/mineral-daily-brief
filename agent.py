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

## 什么时候**不要**调工具
打招呼、问你「你能做什么」、让你解释概念 —— 这类不需要外部数据的消息，直接回答就行。
不要为了显得勤快就去拉一遍价格，那既慢又费钱。

## 只回答最新那一条
每轮只管用户**这次**问的事。**不要**在回答里回头把之前几轮的问题再答一遍 ——
用户已经看过那些答案了，重复一遍只会让新内容被淹没。
上文里有过的数据可以直接引用，但不要重复调用已经调过的工具去重新取一遍。

## 检索不理想时必须换词重试，不许硬交
新闻工具返回 `low_confidence: true`、或者结果明显跟目标不相关时，**不要**就这么出简报。
换成**项目名/矿名/品种名**再搜一轮：实测「公司名」往往只命中股票分析稿，
「项目名」才命中行业媒体（查 "Pilbara Minerals" 全是推广稿，改查 "Pilgangoora" 才有真报道）。
换过一次再不行，就在回答里明说「没检索到可靠报道」并列出已尝试的词。

## 硬性要求
- 每个数字都要来自工具返回，不要凭记忆或心算。价格要标数据源。
- 工具说拿不到，就说拿不到（全文不可用 / 没识别出储量表 / 低置信度），
  不要用别的数字顶替，也不要用常识补。
- 推算值（字段名带 derived）要说明是推算的。
- **直接用中文给结果**。不要写「我已经收集够了」「下面是简报」这类开场白，
  更不要用英文过渡句（会出现 "I have enough to build the briefing. Here it is." 这种）。
  第一行就该是标题或答案本身。
- 简洁。用户问一个数字就给那个数字和出处，别铺开一大段。
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
    """从 MCP 工具的 input_schema 生成 pydantic 模型，供 StructuredTool 用。

    数组类型这里放宽成 `list | str`：实测模型会把它写成 "A,B" 这种字符串，
    而 pydantic 校验发生在**模型响应解析阶段**（还没进 ToolNode），
    一旦失败会直接打断整轮对话、连回答都没有。放宽 schema + 在 wrapper 里归一化，
    比让一次格式小错炸掉整轮划算。这类出入在别的参数上也可能出现，先兜住数组这一类。
    """
    schema = getattr(tool, "input_schema", None) or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    types = {
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list | str,
        "object": dict,
    }
    fields = {
        k: (types.get(v.get("type", "string"), str), ... if k in required else v.get("default"))
        for k, v in props.items()
    }
    return create_model(f"{tool.name.replace('-', '_')}_args", **fields)


def make_tool(client: Client, tool) -> StructuredTool:
    """把一个 MCP 工具包成 LangChain 工具。

    闭包持有 client —— 所以 client 必须活到整场对话结束，工具调用时连接还在。
    """
    props = (getattr(tool, "input_schema", None) or {}).get("properties") or {}
    array_params = {k for k, v in props.items() if v.get("type") == "array"}

    async def call(**kwargs):
        # 数组参数被写成字符串时补一次归一化，别让它带进 MCP 层
        for k in array_params:
            v = kwargs.get(k)
            if isinstance(v, str):
                kwargs[k] = [s.strip() for s in v.replace("，", ",").split(",") if s.strip()]

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
    from langchain_core.messages import AIMessage, HumanMessage

    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: MessagesState):
        msgs = state["messages"]
        try:
            return {"messages": [await llm_with_tools.ainvoke(msgs)]}
        except Exception as exc:  # noqa: BLE001
            # 模型把工具参数写错格式时，报错发生在**响应解析阶段**（还没进 ToolNode），
            # ToolNode 的 handle_tool_errors 兜不住，整轮会直接死掉、连回答都没有。
            # 这里加一条纠正提示重试一次；nudge 只用于本次调用，不写回 state。
            try:
                nudged = [
                    *msgs,
                    HumanMessage(
                        content=f"你上一次的工具调用参数格式不对（{exc}）。"
                        "请重新调用同一工具，严格按参数类型传值。"
                    ),
                ]
                return {"messages": [await llm_with_tools.ainvoke(nudged)]}
            except Exception:
                # 重试也失败，说明多半**不是**「参数格式」问题（密钥无效、网络不通、
                # 请求头非法都会走到这里）。把原始错误原样抛出去，让用户看到真正的原因。
                # 早先这里回一句「工具调用参数解析失败」，把密钥错误之类的全盖成同一句话，
                # 排查时被结结实实带偏过一次。
                raise exc

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
