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
import re
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

## 每轮先判断意图，再决定要不要调工具
回话之前先问自己一句：**用户这句话需要外部数据吗？**

- **不需要**（打招呼、问你能做什么、让你解释概念、闲聊）→ 直接回答，**一个工具都不要调**。
- **需要**（问价格、要新闻、要储量、要简报）→ 才去调。

⚠️ **不要被上文带着走**。实测过一个典型错误：上一轮在查铁矿石走势，这一轮用户只说「你好」，
模型又去调了一遍 `get_trend(铁矿石)` —— 那是把打招呼当成了「继续更新刚才那份数据」。
**历史里有数据不等于这一轮还要去取**，用户这句「你好」就是打招呼。

## 只回答最新那一条
每轮只管用户**这次**问的事。**不要**在回答里回头把之前几轮的问题再答一遍 ——
用户已经看过那些答案了，重复一遍只会让新内容被淹没。
上文里有过的数据可以直接引用，但不要重复调用已经调过的工具去重新取一遍。

## 检索不理想时必须换词重试，不许硬交
新闻工具返回 `low_confidence: true`、或者结果明显跟目标不相关时，**不要**就这么出简报。
换成**项目名/矿名/品种名**再搜一轮：实测「公司名」往往只命中股票分析稿，
「项目名」才命中行业媒体（查 "Pilbara Minerals" 全是推广稿，改查 "Pilgangoora" 才有真报道）。
换过一次再不行，就在回答里明说「没检索到可靠报道」并列出已尝试的词。

## 出简报时的固定结构
用户要「简报 / 日报」时，**必须**用下面这个结构，不要自己另起一套小节名：

    # 矿权日报 · <对象> · <日期>

    ## 一、新闻摘要
    （3-5 条，每条一句话概括 + 来源）

    ## 二、储量数据
    ## 三、价格走势
    ## 四、风险提示
    ## 引用来源
    （编号列出所有用到的链接）

四节里**每一节都要有**。拿不到就在那一节里写明「本次未取得」和原因，
**绝不许悄悄跳过整节** —— 漏掉一节而不说，比明说拿不到糟得多。

**「储量数据」这一节必须真的去查**，不能因为新闻里没提到就当它不存在：

1. 先 `mineral-pdf_known_reports` 查有没有登记该公司的技术报告
2. 有就把 PDF 链接喂给 `mineral-pdf_extract_resources` 抽 Indicated / Inferred

查完确实没有，再写「本次未取得储量数据（原因：…）」。

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


def _json_type_of(spec: dict) -> str:
    """从一段 JSON Schema 里判断类型。

    ⚠️ 坑在这里：`Annotated[X | None, Field(...)]` 生成的是
    `{"anyOf": [{"type": "integer"}, {"type": "null"}]}`，
    **没有顶层 `type` 字段**。直接 `spec.get("type", "string")` 会拿到默认值 "string"，
    把所有可选参数一律声明成字符串 —— 数组参数因此被拒（"Input should be a valid list"）。
    """
    t = spec.get("type")
    if t is None:
        for opt in spec.get("anyOf") or spec.get("oneOf") or []:
            if opt.get("type") and opt["type"] != "null":
                return opt["type"]
    return t or "string"


def _args_schema(tool) -> type:
    """从 MCP 工具的 input_schema 生成 pydantic 模型，供 StructuredTool 用。

    数组类型放宽成 `list | str`：实测模型会把它写成 "A,B" 这种字符串，
    而 pydantic 校验发生在**模型响应解析阶段**（还没进 ToolNode），
    一旦失败会直接打断整轮对话。放宽 schema + 在 wrapper 里归一化更划算。
    """
    schema = getattr(tool, "input_schema", None) or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    types = {
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list | str | None,   # 模型有时显式传 null，别为这个把整轮打断
        "object": dict,
    }
    fields = {
        k: (
            types.get(_json_type_of(v), str),
            ... if k in required else v.get("default"),
        )
        for k, v in props.items()
    }
    return create_model(f"{tool.name.replace('-', '_')}_args", **fields)


def make_tool(client: Client, tool) -> StructuredTool:
    """把一个 MCP 工具包成 LangChain 工具。

    闭包持有 client —— 所以 client 必须活到整场对话结束，工具调用时连接还在。
    """
    props = (getattr(tool, "input_schema", None) or {}).get("properties") or {}
    # ⚠️ 这里也要用 _json_type_of：和 _args_schema 是同一个 anyOf 坑，
    # 直接 v.get("type") 对可选参数永远拿到 None，归一化就成了死代码。
    array_params = {k for k, v in props.items() if _json_type_of(v) == "array"}

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


# 纯打招呼 / 纯客套。**只匹配整句就是问候的**，所以「你好，顺便查下铜价」不会被误判。
_CHITCHAT_RE = re.compile(
    r"^(你好|您好|哈喽|嗨|在吗|早上好|中午好|晚上好|谢谢|多谢|感谢|辛苦了|"
    r"hi|hello|hey|yo|thanks|thank you|ok|okay|好的|收到)[\s!！。.~～,，、?？]*$",
    re.I,
)


def is_chitchat(text: str) -> bool:
    return bool(_CHITCHAT_RE.match((text or "").strip()))


INTENT_SYSTEM = """你在做「意图改写」，不是回答问题。看完最近几轮对话和用户最新一句话，只输出一个 JSON：

{"needs_data": true 或 false, "rewritten": "改写后的一句话"}

needs_data —— 这句话需要外部数据吗？
  true  ：要价格、要新闻、要储量、要简报，或让查某个品种/公司/项目
  false ：打招呼、闲聊、问你能力、让你解释概念

rewritten —— 把用户最新这句话改写成**一句自包含的请求**：
  · 指代词还原：「它」「这个」「那家」→ 上文说的实际对象
  · 省略补全：上文在问铁矿石，这轮只说「再算 60 天」→ 补成「铁矿石近 60 天走势」
  · 本来就自包含 → 原样返回；寒暄类也原样返回

只输出 JSON。不要解释，不要代码块。"""


class AgentState(MessagesState):
    """比 MessagesState 多两个字段：意图判断的结果。

    注意 intent 的结果**不写进 messages**，只在 agent 节点里临时用 ——
    否则每轮都会往历史里塞一条内部消息。
    """

    needs_data: bool
    rewritten: str


def _parse_intent(text: str) -> dict:
    """从模型输出里抠 JSON。容忍 ``` 包裹和前后多余的话。"""
    t = re.sub(r"```(?:json)?", "", (text or "").strip()).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        d = json.loads(t[i: j + 1])
    except json.JSONDecodeError:
        return {}
    return {
        "needs_data": bool(d.get("needs_data", True)),
        "rewritten": str(d.get("rewritten") or "").strip(),
    }


async def _judge_intent(llm: ChatOpenAI, messages: list) -> dict:
    """判断意图 + 改写输入。

    判不出来时一律按「需要数据」走 —— 宁可多调一次工具，
    也不要因为误判成寒暄而答不出用户真正要的东西。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    last_user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if last_user is None:
        return {"needs_data": True, "rewritten": ""}
    # 明确是纯问候就别再花一次模型调用
    if is_chitchat(last_user.content):
        return {"needs_data": False, "rewritten": last_user.content}
    try:
        # 只喂最近几轮：改写需要上文来还原指代词，但不需要整段历史
        r = await llm.ainvoke(
            [SystemMessage(content=INTENT_SYSTEM), *messages[-8:]],
            max_tokens=220,
            temperature=0,
        )
        return _parse_intent(r.content) or {"needs_data": True, "rewritten": ""}
    except Exception:  # noqa: BLE001 - 意图判断失败不该拖垮整轮
        return {"needs_data": True, "rewritten": ""}


def build_graph(llm: ChatOpenAI, tools: list[StructuredTool]):
    """三个节点：intent（判断意图 + 改写输入）→ agent（调模型）→ tools（执行工具）。

    intent 节点是后加的：原来只有 agent + tools，模型在连续对话里会被上文带偏 ——
    上一轮问过铁矿石走势，这一轮只说「你好」，它又去调一遍 get_trend(铁矿石)。
    光在提示词里写「打招呼别调工具」压不住，得在流程上先判一次意图。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm_with_tools = llm.bind_tools(tools) if tools else llm

    async def intent_node(state: AgentState):
        d = await _judge_intent(llm, state["messages"])
        return {"needs_data": d["needs_data"], "rewritten": d.get("rewritten") or ""}

    async def agent_node(state: AgentState):
        msgs = list(state["messages"])
        # 用改写后的请求替换最后一条用户消息。只影响本次调用，不写回 state。
        rw = state.get("rewritten") or ""
        if rw and msgs and isinstance(msgs[-1], HumanMessage) and rw != msgs[-1].content:
            msgs = [*msgs[:-1], HumanMessage(content=rw)]

        # 判断为「不需要数据」时不绑工具 —— 从机制上杜绝工具调用
        model = llm_with_tools if state.get("needs_data", True) else llm
        try:
            return {"messages": [await model.ainvoke(msgs)]}
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
                return {"messages": [await model.ainvoke(nudged)]}
            except Exception:
                # 重试也失败，说明多半**不是**「参数格式」问题（密钥无效、网络不通、
                # 请求头非法都会走到这里）。把原始错误原样抛出去，让用户看到真正的原因。
                # 早先这里回一句「工具调用参数解析失败」，把密钥错误之类的全盖成同一句话，
                # 排查时被结结实实带偏过一次。
                raise exc

    def route(state: AgentState):
        """模型这一轮调工具了就去 tools，否则收尾。"""
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(AgentState)
    graph.add_node("intent", intent_node)
    graph.add_node("agent", agent_node)
    graph.add_edge(START, "intent")
    graph.add_edge("intent", "agent")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("agent", route, ["tools", END])
        graph.add_edge("tools", "agent")
    else:
        graph.add_edge("agent", END)
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
