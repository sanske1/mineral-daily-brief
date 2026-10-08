"""mining-news-mcp —— 矿业新闻聚合 MCP server。

工具：search / fetch_article / corpus_stats

两条取数通道，互补：

  A. 常驻 RSS（mining.com / The Northern Miner）
     —— 全站最新，正文可抓，是本地语料的底子。翻页可回溯数年
        （实测 mining.com 每页 36 条，paged=120 能回到约两年前）。

  B. Google News 查询 RSS（按 query 实时生成，中英各一路）
     —— 解决 A 的致命短板：A 是**全站最新**，某个具体标的（比如 Pilbara）
        可能几十天都没上过头条。B 按主题取，任意公司/品种都有当天报道。
        但每个查询硬上限 100 条，所以不同查询词的结果**必须累积**才有覆盖面。

关于全文：Google News 的链接**不会**重定向到原文（实测跟随重定向后停在
news.google.com 自己的 592KB JS 页面上）。所以 B 通道的条目只有标题+摘要+来源，
标记 origin='gnews'；fetch_article 遇到这类链接会明说「拿不到全文」，
而不是把 Google 的页面当成文章正文塞给模型。宁可承认拿不到，也不编。
"""

from __future__ import annotations

import html as html_mod
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated, Any

# ---- 静音 FastMCP 的启动日志 ----
# FastMCP 默认会往 stderr 打一堆启动日志和 rich 横幅。它**走 stderr**，所以不会污染
# stdio 协议；但会把 CLI 输出搞得很吵。这三个开关必须在 import fastmcp **之前**生效。
import os as _os_for_logging

_os_for_logging.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
_os_for_logging.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
_os_for_logging.environ.setdefault("FASTMCP_ENABLE_RICH_LOGGING", "false")

from fastmcp import FastMCP
from pydantic import Field

import cache

mcp = FastMCP(
    name="mining-news",
    instructions=(
        "矿业新闻聚合：全文检索 + 正文抓取。"
        "数据源为 mining.com、The Northern Miner，以及按查询实时生成的 Google News 源。"
    ),
)

# ---- 通道 A：常驻 RSS ----
FEEDS: list[dict[str, Any]] = [
    {"name": "mining.com", "url": "https://www.mining.com/feed/", "pages": 10},
    {"name": "northernminer.com", "url": "https://www.northernminer.com/feed/", "pages": 3},
]

# ---- 通道 B：Google News 查询源。中英各一路，覆盖跨语言报道。 ----
GOOGLE_LOCALES = [
    {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
]

STALE_AFTER_SECONDS = 6 * 3600
MIN_PRECISE_HITS = 3  # 精确召回少于这个数，就认为本地库覆盖不足，触发实时补抓

_META_PROBE = re.compile(r'<meta[^>]+(?:property|name|itemprop)="([^"]+)"[^>]+content="([^"]*)"', re.I)

# 正文容器，按优先级尝试。mining.com 实测是 post-inner-content。
_BODY_SELECTORS = [
    "post-inner-content",
    "entry-content",
    "post-content",
    "article-content",
    "article-body",
]


# ===========================================================================
# 中英矿业术语词典
#
# 解决的问题：本地常驻语料是英文的，用户却用中文提问。用户问「锂矿」，
# 英文语料里的 "lithium" 不该被漏掉。
#
# 这是**确定性**的词表扩展，不调用任何模型 —— 因此这个 server 单独塞进
# Claude Desktop 也能工作，不需要带 LLM 依赖。
# 词表宁可短而准：错扩一个词会把不相干的文章拉进结果，比漏扩更难排查。
# ===========================================================================

LEXICON: dict[str, list[str]] = {
    "锂": ["lithium", "li"],
    "锂矿": ["lithium", "spodumene", "pegmatite"],
    "锂辉石": ["spodumene"],
    "碳酸锂": ["lithium carbonate"],
    "氢氧化锂": ["lithium hydroxide"],
    "铜": ["copper"],
    "镍": ["nickel"],
    "锌": ["zinc"],
    "铅": ["lead"],
    "钴": ["cobalt"],
    "铝": ["aluminium", "aluminum"],
    "稀土": ["rare earth", "rare earths", "REE"],
    "石墨": ["graphite"],
    "铁矿石": ["iron ore"],
    "铁矿": ["iron ore"],
    "黄金": ["gold"],
    "银": ["silver"],
    "铀": ["uranium"],
    "钾肥": ["potash"],
    "储量": ["resource", "reserve", "mineral resource"],
    "资源量": ["mineral resource", "resource"],
    "品位": ["grade"],
    "产量": ["production", "output"],
    "出口": ["export"],
    "进口": ["import"],
    "关税": ["tariff", "duty"],
    "价格": ["price"],
    "涨价": ["price rise", "rally", "surge"],
    "跌价": ["price fall", "decline", "slump"],
    "政策": ["policy", "regulation"],
    "监管": ["regulator", "regulation"],
    "并购": ["acquisition", "merger"],
    "扩产": ["expansion", "ramp up"],
    "停产": ["suspension", "halt", "shutdown"],
    "罢工": ["strike"],
    "矿权": ["mining rights", "tenement", "mining lease"],
    "勘探": ["exploration", "drilling"],
    "矿山": ["mine", "mining"],
    "项目": ["project"],
    "审批": ["approval", "permit"],
    "环保": ["environmental", "ESG"],
}

_REVERSE: dict[str, list[str]] = {}
for _zh, _ens in LEXICON.items():
    for _en in _ens:
        _REVERSE.setdefault(_en.lower(), []).append(_zh)


def expand_terms(token: str) -> list[str]:
    """给一个查询词，返回可接受的替代词（不含原词本身）。"""
    if not token:
        return []
    low = token.lower().strip()
    out: list[str] = []

    if token in LEXICON:
        out.extend(LEXICON[token])
    if low in _REVERSE:
        out.extend(_REVERSE[low])
    # 中文长词里含短词的情况：「碳酸锂价格」应同时扩展出 lithium carbonate 和 price
    if token not in LEXICON:
        for zh, ens in LEXICON.items():
            if len(zh) >= 2 and zh in token and zh != token:
                out.extend(ens)

    seen = {low}
    deduped = []
    for w in out:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            deduped.append(w)
    return deduped


# ===========================================================================
# RSS 解析
# ===========================================================================


def _strip_tags(raw: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</p>", "\n\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt)
    return re.sub(r"[ \t]+", " ", txt.replace("\xa0", " ")).strip()


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _child_text(node: ET.Element, tag: str, ns: str | None = None) -> str | None:
    child = node.find(f"{{{ns}}}{tag}") if ns else node.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _parse_feed(xml_text: str, source: str) -> list[dict[str, Any]]:
    """解析 RSS 2.0 / Atom。字段缺失不报错，尽力取。"""
    text = xml_text.strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        idx = text.find("<")
        if idx <= 0:
            return []
        try:
            root = ET.fromstring(text[idx:])
        except ET.ParseError:
            return []

    entries: list[dict[str, Any]] = []
    for it in root.iter("item"):
        link = _child_text(it, "link")
        if not link:
            guid = it.find("guid")
            link = guid.text.strip() if guid is not None and guid.text else None
        title = _child_text(it, "title")
        if not (link and title):
            continue
        publisher = _child_text(it, "source")  # Google News 用它标明发布方
        desc = _child_text(it, "description")
        entries.append(
            {
                "url": link,
                "title": html_mod.unescape(_strip_tags(title)),
                "summary": _strip_tags(desc)[:600] if desc else None,
                "published": _parse_date(
                    _child_text(it, "pubDate")
                    or _child_text(it, "date", "http://purl.org/dc/elements/1.1/")
                ),
                "author": _child_text(it, "creator", "http://purl.org/dc/elements/1.1/"),
                "category": (it.find("category").text if it.find("category") is not None else None),
                "publisher": publisher,
                "source": source,
            }
        )

    if not entries:  # 退回 Atom
        atom = "http://www.w3.org/2005/Atom"
        for it in root.iter(f"{{{atom}}}entry"):
            link_node = it.find(f"{{{atom}}}link")
            title_node = it.find(f"{{{atom}}}title")
            if link_node is None or title_node is None:
                continue
            summ = it.find(f"{{{atom}}}summary")
            upd = it.find(f"{{{atom}}}updated")
            entries.append(
                {
                    "url": link_node.get("href"),
                    "title": _strip_tags(title_node.text or ""),
                    "summary": _strip_tags(summ.text or "")[:600] if summ is not None else None,
                    "published": _parse_date(upd.text if upd is not None else None),
                    "author": None,
                    "category": None,
                    "publisher": None,
                    "source": source,
                }
            )
    return entries


def _store_entries(conn, entries: list[dict[str, Any]], *, origin: str, now_iso: str) -> int:
    inserted = 0
    for e in entries:
        if cache.upsert_article(
            conn,
            url=e["url"],
            title=e["title"],
            source=e.get("publisher") or e["source"],
            author=e.get("author"),
            category=e.get("category"),
            published=e.get("published"),
            summary=e.get("summary"),
            now=now_iso,
            origin=origin,
        ):
            inserted += 1
    return inserted


def _ingest(conn, *, pages_override: int | None = None, force: bool = False) -> dict[str, Any]:
    """通道 A：翻页抓常驻 RSS。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    report: dict[str, Any] = {"sources": [], "inserted": 0, "seen": 0, "errors": []}

    for feed in FEEDS:
        pages = pages_override or feed["pages"]
        src_inserted = src_seen = 0
        for page in range(1, pages + 1):
            url = feed["url"] if page == 1 else f"{feed['url']}?paged={page}"
            try:
                text, _ = cache.fetch_text(url, ttl=0 if force else 900, retries=2)
            except cache.FetchError as exc:
                if exc.status == 404:
                    break  # 翻到底了，正常终止
                report["errors"].append(f"{feed['name']} p{page}: {exc.reason}")
                break
            entries = _parse_feed(text, feed["name"])
            if not entries:
                break
            src_seen += len(entries)
            src_inserted += _store_entries(conn, entries, origin="rss", now_iso=now_iso)
        report["sources"].append(
            {"source": feed["name"], "pages": pages, "seen": src_seen, "inserted": src_inserted}
        )
        report["inserted"] += src_inserted
        report["seen"] += src_seen

    conn.commit()
    report["elapsed_s"] = round(time.time() - t0, 2)
    return report


def _google_feed_url(query: str, locale: dict[str, str], days: int | None = None) -> str:
    """拼 Google News 查询 URL。

    带 when:Nd 是因为实测：Google News 按相关性返回，结果里混着好几年前的旧文
    （查「锂矿」捞到 2017、2021、2023 的报道）。不加时间限定的话，一篇九年前的
    文章可能被当成「今日简报」的素材。
    """
    q = query + (f" when:{days}d" if days else "")
    return (
        f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(q)}"
        f"&hl={locale['hl']}&gl={locale['gl']}&ceid={locale['ceid']}"
    )


def _ingest_google(conn, query: str, *, days: int | None = None, force: bool = False) -> dict[str, Any]:
    """通道 B：按查询实时抓 Google News（中英各一路）。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    report: dict[str, Any] = {"locales": [], "inserted": 0, "seen": 0, "errors": []}

    for loc in GOOGLE_LOCALES:
        url = _google_feed_url(query, loc, days)
        try:
            text, _ = cache.fetch_text(url, ttl=0 if force else 900, retries=1, timeout=20)
        except cache.FetchError as exc:
            report["errors"].append(f"gnews {loc['ceid']}: {exc.reason}")
            continue
        entries = _parse_feed(text, f"news.google.com/{loc['ceid']}")
        report["seen"] += len(entries)
        n = _store_entries(conn, entries, origin="gnews", now_iso=now_iso)
        report["inserted"] += n
        report["locales"].append({"locale": loc["ceid"], "seen": len(entries), "inserted": n})

    conn.commit()
    report["elapsed_s"] = round(time.time() - t0, 2)
    return report


def _ensure_fresh(conn, *, max_age_hours: float = 6.0, min_rows: int = 60) -> dict[str, Any] | None:
    """本地常驻语料为空或过旧时补抓通道 A。返回 None 表示无需动作。"""
    total = conn.execute("SELECT COUNT(*) c FROM articles WHERE origin='rss'").fetchone()["c"]
    if total < min_rows:
        return _ingest(conn)
    row = conn.execute("SELECT MAX(first_seen) m FROM articles WHERE origin='rss'").fetchone()
    newest = row["m"] if row else None
    if not newest:
        return _ingest(conn)
    try:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(newest)).total_seconds() / 3600
    except ValueError:
        return _ingest(conn)
    return _ingest(conn) if age_h > max_age_hours else None


# ===========================================================================
# 正文抓取
# ===========================================================================


def _extract_body(html_text: str) -> str:
    """抽正文段落。按容器类名优先级尝试，全落空则退化为全页 <p>。"""
    for cls in _BODY_SELECTORS:
        m = re.search(
            rf'<div[^>]*class="[^"]*{cls}[^"]*"[^>]*>(.*?)'
            rf'(?=<div[^>]*class="[^"]*(?:post-more-news|share|comments)|</article>)',
            html_text,
            re.S | re.I,
        ) or re.search(rf'<div[^>]*class="[^"]*{cls}[^"]*"[^>]*>(.*)', html_text, re.S | re.I)
        if not m:
            continue
        chunk = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", m.group(1), flags=re.S | re.I)
        paras = [_strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", chunk, re.S | re.I)]
        text = "\n\n".join(p for p in paras if len(p) > 30)
        if len(text) > 300:
            return text

    paras = [_strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html_text, re.S | re.I)]
    return "\n\n".join(p for p in paras if len(p) > 60)


def _metas(html_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for prop, content in _META_PROBE.findall(html_text):
        out.setdefault(prop.lower(), html_mod.unescape(content).strip())
    return out


def _fmt(r: dict) -> dict[str, Any]:
    """把库里的一行整理成给模型看的结果项。

    source_tier 在这里很关键：直连 RSS 是媒体一手报道（可抓正文），
    Google News 是聚合转引（只有标题摘要）。二者不该被等同看待。
    """
    is_direct = r["origin"] == "rss"
    return {
        "title": r["title"],
        "url": r["url"],
        "source": r["source"],
        "published": r["published"],
        "category": r["category"],
        "summary": r["summary"],
        "full_text_available": is_direct,
        "body_cached": bool(r["has_body"]),
        "source_tier": cache.TIER_OFFICIAL if is_direct else cache.TIER_RELAY,
        "source_note": (
            "媒体直连 RSS，一手报道，可抓正文"
            if is_direct
            else "Google News 聚合转引，仅标题与摘要，链接不指向发布方原文"
        ),
    }


# ===========================================================================
# 工具
# ===========================================================================


@mcp.tool()
def search(
    query: Annotated[str, Field(description="检索词，中英均可，如 'Pilbara 锂矿'、'lithium price'")],
    days: Annotated[int, Field(description="回溯天数", ge=1, le=3650)] = 30,
    limit: Annotated[int, Field(description="最多返回条数", ge=1, le=100)] = 15,
    live: Annotated[
        bool, Field(description="允许按查询实时抓 Google News 补充；关掉则只查本地库")
    ] = True,
) -> dict:
    """检索近 days 天内的矿业新闻。本地精确召回不足时会自动按查询补抓外部源。"""
    conn = cache.connect()
    try:
        baseline = _ensure_fresh(conn)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def query_db(*, require_all: bool, use_window: bool = True) -> list[dict]:
            return cache.search_articles(
                conn,
                query,
                limit=limit,
                since=since if use_window else None,
                expand_fn=expand_terms,
                require_all=require_all,
            )

        # 阶梯 1：精确召回（所有查询词都要命中）
        rows = query_db(require_all=True)
        strategy = "精确匹配（本地库）"
        google_report = None
        time_window_widened = False

        # 阶梯 2：精确召回不足 -> 按查询实时补抓，再精确召回一次。
        # 顺序必须先补抓再考虑宽松召回：反过来的话，宽松召回凑出的几条弱相关结果
        # 会让上游误以为「已经召回充分」，从而跳过真正该取的数据源。
        if len(rows) < MIN_PRECISE_HITS and live:
            google_report = _ingest_google(conn, query, days=days)
            rows = query_db(require_all=True)
            strategy = "精确匹配（本地库 + 实时补充 Google News）"

        # 阶梯 3：仍然没有 -> 宽松召回，如实标注置信度低
        if not rows:
            rows = query_db(require_all=False)
            if rows:
                strategy = "宽松匹配（仅命中部分查询词，置信度低，请人工核对）"

        # 阶梯 4：放宽时间窗。
        # ⚠️ 这会把**很旧**的文章捞出来（实测库里最早的到 2017 年）。所以不只置布尔位，
        # 还把实际日期范围报出来，让上游无法忽略时效性。
        window_note = None
        if not rows:
            rows = query_db(require_all=True, use_window=False)
            if rows:
                time_window_widened = True
                dates = sorted(r["published"] for r in rows if r["published"])
                strategy = "精确匹配（已放宽到全部历史）"
                window_note = (
                    f"⚠️ 近 {days} 天内无命中，已放宽到全部历史。"
                    f"返回结果的发布日期为 {dates[0][:10]} ~ {dates[-1][:10]}，"
                    f"**这不是近期新闻**，写入「今日简报」前必须说明时效性，或改用其他检索词。"
                )

        return {
            "query": query,
            "days": days,
            "since": since,
            "match_strategy": strategy,
            "low_confidence": "置信度低" in strategy,
            "time_window_widened": time_window_widened,
            "time_window_note": window_note,
            "total_returned": len(rows),
            "corpus_size": cache.count_articles(conn),
            "baseline_ingest": baseline,
            "google_ingest": google_report,
            "results": [_fmt(r) for r in rows],
        }
    finally:
        conn.close()


@mcp.tool()
def fetch_article(
    url: Annotated[str, Field(description="文章链接，通常来自 search 的结果")],
) -> dict:
    """抓取一篇新闻的正文全文。Google News 聚合链接无法取得正文，会明确说明。"""
    if "news.google.com" in url:
        # 别把 Google 的跳转页当正文。如实说明，让上游改用该条目的标题+摘要。
        return {
            "url": url,
            "error": "full_text_unavailable",
            "reason": (
                "该条目来自 Google News 聚合源，其链接不会重定向到发布方原文"
                "（实测跟随重定向后停在 news.google.com 自己的页面上），因此无法取得正文。"
                "请改用该条目的 title/summary/source 字段，或挑选 origin 为 RSS 直连源的条目。"
            ),
            "body": None,
            "suggestion": "在本条目的 source 字段里找到发布方，另行检索该发布方的直连文章。",
        }

    conn = cache.connect()
    try:
        row = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        if row and row["body"]:
            return {
                "url": url,
                "title": row["title"],
                "source": row["source"],
                "published": row["published"],
                "from_cache": True,
                "body": row["body"],
                "body_chars": len(row["body"]),
            }

        try:
            html_text, meta = cache.fetch_text(url, ttl=86400, retries=2)
        except cache.FetchError as exc:
            return {
                "url": url,
                "error": "fetch_failed",
                "reason": exc.reason,
                "status": exc.status,
                "body": None,
            }

        body = _extract_body(html_text)
        metas = _metas(html_text)
        title = metas.get("og:title") or (row["title"] if row else None)
        published = metas.get("article:published_time") or (row["published"] if row else None)

        if not body:
            return {
                "url": url,
                "title": title,
                "error": "no_body_extracted",
                "reason": "页面结构与已知正文容器都不匹配，不强行拼凑正文",
                "body": None,
            }

        if row:
            cache.set_body(conn, row["id"], body, datetime.now(timezone.utc).isoformat())
            conn.commit()

        return {
            "url": url,
            "title": title,
            "source": row["source"] if row else None,
            "published": published,
            "image": metas.get("og:image"),
            "from_cache": bool(meta.get("from_cache")),
            "body": body,
            "body_chars": len(body),
        }
    finally:
        conn.close()


@mcp.tool()
def corpus_stats() -> dict:
    """查看本地新闻库的规模与时间跨度（诊断用）。"""
    conn = cache.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(published) lo, MAX(published) hi, "
            "SUM(body IS NOT NULL) with_body FROM articles"
        ).fetchone()
        by_origin = [
            dict(r)
            for r in conn.execute(
                "SELECT origin, COUNT(*) n FROM articles GROUP BY origin ORDER BY n DESC"
            ).fetchall()
        ]
        return {
            "articles": row["n"],
            "with_full_body": row["with_body"],
            "earliest": row["lo"],
            "latest": row["hi"],
            "by_origin": by_origin,
            "db_path": str(cache.db_path()),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
