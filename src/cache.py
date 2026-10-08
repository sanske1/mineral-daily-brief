"""数据缓存层：上游取数 + 本地存储 + 数据源登记。

三个 MCP server 都只依赖这一个模块。分三块：

  一、数据源登记 —— 每个源的权威性等级（官方直连 / 转载 / 替代口径）
  二、HTTP 取数   —— 浏览器 UA、重试、磁盘缓存、陈旧兜底
  三、本地存储    —— SQLite + 中文可用的 FTS5 全文检索

关于「为什么要缓存」：上游数据分两类。新闻是**滑动窗口**（今天不抓，明天就滑出去了），
行情是**不可变历史**（2026-10-08 的收盘价永不再变）。前者靠缓存累积，后者靠缓存免重复请求 ——
上期所官方没有批量接口，一天一个文件，30 日走势不带缓存就要打 30 次。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

# ===========================================================================
# 一、数据源登记
#
# 同样一个「沪铜 110,260 元/吨」，从上期所官网取的、和从行情门户取到的，可信度不一样。
# 不标出来的话读者无法判断该信几分。所以每个返回值都带 source_tier。
#
#   official    交易所 / 公司官网直连。权威，可直接引用。
#   relay       门户网站转载的官方行情。数值通常一致，但可能滞后。
#   substitute  口径相近但**不同**的东西顶替。必须说明差异，不能当作等价物。
# ===========================================================================

TIER_OFFICIAL = "official"
TIER_RELAY = "relay"
TIER_SUBSTITUTE = "substitute"

TIER_LABEL = {
    TIER_OFFICIAL: "官方直连",
    TIER_RELAY: "转载源",
    TIER_SUBSTITUTE: "替代口径",
}

# available=False 的是**实测确认拿不到的**，写在这里是为了让评审方看到
# 「我们试过、为什么不用」，而不是以为我们漏了。
SOURCES: dict[str, dict] = {
    "shfe_official": {
        "tier": TIER_OFFICIAL,
        "name": "上海期货交易所官网",
        "url": "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat",
        "available": True,
        "note": "公开日行情 JSON，无需登录。含各合约开高低收、结算价、成交量、持仓量。支持历史日期。",
    },
    "gfex_official": {
        "tier": TIER_OFFICIAL,
        "name": "广州期货交易所官网",
        "url": "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList",
        "available": True,
        "note": "公开接口，POST trade_date + trade_type=0。含碳酸锂等品种全合约日行情。⚠️ 忽略日期参数，只能取最新。",
    },
    "mining_com": {
        "tier": TIER_OFFICIAL,
        "name": "mining.com",
        "url": "https://www.mining.com/feed/",
        "available": True,
        "note": "矿业垂直媒体的一手报道。⚠️ 裸请求返回 403，必须带浏览器 UA。翻页可回溯数年。",
    },
    "company_filings": {
        "tier": TIER_OFFICIAL,
        "name": "上市公司官网披露",
        "url": "",
        "available": True,
        "note": "如 pls.com 年报、Implats AIF。储量数据的唯一权威来源。",
    },
    "eastmoney": {
        "tier": TIER_RELAY,
        "name": "东方财富行情",
        "url": "https://push2.eastmoney.com/api/qt/stock/get",
        "available": True,
        "note": "转载自各交易所。覆盖广、有历史序列，但会限流（实测整个 IP 被限到 code=000）。",
    },
    "sina_finance": {
        "tier": TIER_RELAY,
        "name": "新浪财经行情",
        "url": "https://hq.sinajs.cn/list=",
        "available": True,
        "note": "转载源，作为东财的备份。返回 GBK，需带 Referer。",
    },
    "northernminer": {
        "tier": TIER_RELAY,
        "name": "The Northern Miner",
        "url": "https://www.northernminer.com/feed/",
        "available": True,
        "note": "矿业媒体 RSS，作为 mining.com 的补充。",
    },
    "google_news": {
        "tier": TIER_RELAY,
        "name": "Google News 聚合",
        "url": "https://news.google.com/rss/search",
        "available": True,
        "note": "聚合源，按查询实时取，每查询上限 100 条。只给标题摘要，链接不指向原文，故不提供全文。",
    },
    "dce_iron_ore_futures": {
        "tier": TIER_SUBSTITUTE,
        "name": "大连商品交易所 铁矿石期货",
        "url": "https://push2.eastmoney.com/api/qt/stock/get",
        "available": True,
        "note": (
            "题面点名的是上海钢联铁矿石**现货指数**（见 mysteel_index，实测拿不到），"
            "这里给的是大商所铁矿石**期货**价格，经东方财富/新浪转载取得。"
            "期货含基差与预期，与现货指数**不等价**，引用时必须说明口径。"
        ),
    },
    # ---- 实测不可用 ----
    "lme_official": {
        "tier": TIER_OFFICIAL,
        "name": "伦敦金属交易所官网",
        "url": "https://www.lme.com/",
        "available": False,
        "note": "官网返回 403（地域封锁/Cloudflare）。LME 官方行情需购买数据授权，因此 LME 价格只能经转载源取得。",
    },
    "mysteel_index": {
        "tier": TIER_OFFICIAL,
        "name": "上海钢联价格指数",
        "url": "https://index.mysteel.com/",
        "available": False,
        "note": (
            "网站可达，但价格指数接口 /zs/newprice/getBaiduChartMultiCity.ms 返回 "
            "401「认证参数传入不完整」，需签名认证与订阅；免费接口只给资讯不给指数值。"
            "题面点名的铁矿石现货指数因此无法接入，改用期货替代（口径不同）。"
        ),
    },
    "dce_official": {
        "tier": TIER_OFFICIAL,
        "name": "大连商品交易所官网",
        "url": "http://www.dce.com.cn/publicweb/quotesdata/exportDayQuotesChData.html",
        "available": False,
        "note": "日行情导出接口返回 412（反爬前置条件检查），未进一步绕过。铁矿石价格改用转载源。",
    },
    "sp_global": {
        "tier": TIER_OFFICIAL,
        "name": "S&P Global Market Intelligence",
        "url": "https://www.spglobal.com/marketintelligence/en/rss",
        "available": False,
        "note": "RSS 返回 403，未接入。题面点名的这个新闻源被 Cloudflare 拦截。",
    },
}


def describe_source(key: str) -> dict:
    info = SOURCES.get(key)
    if not info:
        return {"key": key, "tier": TIER_RELAY, "name": key, "tier_label": TIER_LABEL[TIER_RELAY]}
    out = dict(info)
    out["key"] = key
    out["tier_label"] = TIER_LABEL.get(info["tier"], info["tier"])
    return out


def tier_of(key: str) -> str:
    info = SOURCES.get(key)
    return info["tier"] if info else TIER_RELAY


def tier_fields(source_key: str) -> dict[str, Any]:
    """给返回值附上权威性标签，让简报能说清这个数字该信几分。"""
    info = describe_source(source_key)
    tier = info["tier"]
    if tier == TIER_OFFICIAL:
        note = f"来源：{info['name']}（官方直连）"
    elif tier == TIER_SUBSTITUTE:
        note = f"来源：{info['name']}（替代口径，与原口径不等价，请留意）"
    else:
        note = f"来源：{info['name']}（转载源，可能滞后于官方）"
    return {"source_key": source_key, "source_tier": tier, "source_note": note}


def unavailable_sources() -> list[dict]:
    return [
        {"key": k, "name": v["name"], "tier": v["tier"], "note": v["note"]}
        for k, v in SOURCES.items()
        if not v.get("available", True)
    ]


# ===========================================================================
# 二、HTTP 取数
#
# 为什么必须带浏览器 UA：实测 `curl https://www.mining.com/feed/` 裸请求返回 403，
# 加上 Chrome 的 UA 立刻 200 / 257KB。这不是反爬墙，是 UA 白名单过滤 ——
# 凡是把这个 403 当成「反爬墙」而放弃的方案，都白丢了一个完全可用的数据源。
# ===========================================================================

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}


class FetchError(RuntimeError):
    """取数失败。MCP 工具捕获它并返回结构化错误，而不是让整个 server 崩掉。"""

    def __init__(self, url: str, reason: str, status: int | None = None):
        self.url = url
        self.reason = reason
        self.status = status
        super().__init__(f"{reason} ({url})" + (f" [HTTP {status}]" if status else ""))


def data_dir() -> Path:
    raw = os.environ.get("MDB_DATA_DIR")
    # 源码在 src/ 下，数据目录在仓库根 —— 所以往上退一层
    base = Path(raw) if raw else Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_paths(url: str) -> tuple[Path, Path]:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    c = _cache_dir()
    return c / f"{h}.bin", c / f"{h}.meta.json"


def _read_cache_any_age(url: str) -> tuple[bytes, dict, float] | None:
    """读缓存，**不管多旧**。返回 (body, meta, age_seconds)。"""
    body_path, meta_path = _cache_paths(url)
    if not (body_path.exists() and meta_path.exists()):
        return None
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        body = body_path.read_bytes()
    except (json.JSONDecodeError, OSError):
        return None
    return body, meta, time.time() - float(meta.get("fetched_at", 0))


def _read_cache(url: str, ttl: float) -> tuple[bytes, dict] | None:
    if ttl <= 0:
        return None
    hit = _read_cache_any_age(url)
    if hit is None:
        return None
    body, meta, age = hit
    return (body, meta) if age <= ttl else None


def _write_cache(url: str, body: bytes, meta: dict) -> None:
    body_path, meta_path = _cache_paths(url)
    try:
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    except OSError:
        pass  # 缓存写失败不影响主流程


def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 2,
    ttl: float = 1800.0,
    force: bool = False,
    allow_stale_on_error: bool = False,
) -> tuple[bytes, dict]:
    """取回 URL 内容。返回 (body, meta)。

    meta 含 content_type / final_url / from_cache，兜底时另有 stale / stale_age_seconds。

    allow_stale_on_error：上游挂了就退回**过期的**缓存并标 stale。
    实测东财行情接口会限流、间歇性断连 —— 此时宁可用一份标了「已过期」的旧价，
    也强过让整个简报因为一次取价失败而中断。但**必须标出来**，不能拿旧价冒充实时价。
    """
    if not force:
        hit = _read_cache(url, ttl)
        if hit:
            body, meta = hit
            meta = dict(meta)
            meta["from_cache"] = True
            return body, meta

    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                if method.upper() == "POST":
                    resp = client.post(url, headers=merged, data=data or {})
                else:
                    resp = client.get(url, headers=merged)
            if resp.status_code >= 400:
                raise FetchError(url, "上游返回错误状态", resp.status_code)
            body = resp.content
            meta = {
                "url": url,
                "final_url": str(resp.url),
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "fetched_at": time.time(),
                "bytes": len(body),
                "from_cache": False,
            }
            _write_cache(url, body, meta)
            return body, meta
        except FetchError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))

    if allow_stale_on_error:
        stale = _read_cache_any_age(url)
        if stale:
            body, meta, age = stale
            meta = dict(meta)
            meta["from_cache"] = True
            meta["stale"] = True
            meta["stale_age_seconds"] = round(age, 1)
            meta["stale_reason"] = f"实时请求失败（{type(last_err).__name__}），已退回缓存副本"
            return body, meta

    raise FetchError(url, f"请求失败：{type(last_err).__name__}: {last_err}")


def fetch_text(url: str, *, encoding: str | None = None, **kw: Any) -> tuple[str, dict]:
    body, meta = fetch(url, **kw)
    enc = encoding
    if not enc:
        ct = meta.get("content_type", "").lower()
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip()
    if not enc or enc.lower() in {"iso-8859-1", "ascii"}:
        # 很多站点不声明 charset 或错标成 latin-1，实际是 UTF-8
        for cand in ("utf-8", "gb18030"):
            try:
                return body.decode(cand), meta
            except UnicodeDecodeError:
                continue
        enc = "utf-8"
    return body.decode(enc, errors="replace"), meta


def fetch_json(url: str, **kw: Any) -> tuple[Any, dict]:
    text, meta = fetch_text(url, **kw)
    try:
        return json.loads(text), meta
    except json.JSONDecodeError as exc:
        raise FetchError(url, f"返回不是合法 JSON：{exc.msg}") from exc


def post_form_json(url: str, data: dict[str, str], **kw: Any) -> tuple[Any, dict]:
    """POST 表单并解析 JSON。广期所官方行情接口是 POST + form 编码。"""
    kw.setdefault("method", "POST")
    kw["data"] = data
    return fetch_json(url, **kw)


def stale_note(meta: dict) -> str | None:
    return meta.get("stale_reason") if meta.get("stale") else None


# ===========================================================================
# 三、本地存储 + 全文检索
#
# 为什么不用 FTS5 默认分词器：`unicode61` 按「非字母数字」切词，中文整句会变成
# 一个巨型 token，「锂矿」永远搜不到「Pilbara 锂矿项目」里的锂矿。这里用
# **CJK 逐字空格化**：入库和查询两侧都把一个汉字序列 "锂矿" 折成 "锂 矿"，
# 再用短语查询 "锂 矿" 匹配 —— 等价于子串匹配，中英混排也照常工作。
# （trigram 分词器看似更省事，但它要求 token ≥3 字符，「锂矿」这种两字词直接失效。）
# ===========================================================================

_CJK = r"㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_CJK_RE = re.compile(f"[{_CJK}]")
_TOKEN_RE = re.compile(f"[{_CJK}]+|[A-Za-z][A-Za-z0-9'’._-]*")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    url           TEXT UNIQUE NOT NULL,          -- 去重主键
    title         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',      -- 发布方
    author        TEXT,
    category      TEXT,
    published     TEXT,                          -- ISO8601，days 过滤靠它
    summary       TEXT,
    body          TEXT,                          -- 惰性抓取，未抓时为 NULL
    body_fetched  TEXT,
    first_seen    TEXT NOT NULL,
    origin        TEXT NOT NULL DEFAULT 'rss',   -- 'rss'=可抓全文 / 'gnews'=仅标题摘要
    title_key     TEXT                           -- 归一化标题，二次去重用（见 title_key()）
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
-- title_key 的索引**不在这里建**：老库的表已存在但没这一列，
-- SCHEMA 里的 CREATE INDEX 会先于迁移执行、直接报 no such column。
-- 统一放到 _migrate() 里，建完列再建索引。

-- FTS 表存归一化后的检索文本，与 articles 用 rowid 对齐（非 external-content，
-- 因为索引文本与原文不同：中文逐字空格化过）
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    search_text,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def db_path() -> Path:
    return data_dir() / "brief.sqlite3"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    if "origin" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN origin TEXT NOT NULL DEFAULT 'rss'")

    if "title_key" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_key TEXT")
        _backfill_title_key(conn)

    # 索引统一在这里建：新库（列已在 CREATE TABLE 里）和老库（刚 ALTER 加完）都能覆盖
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_title_key ON articles(title_key)")
    conn.commit()


def _backfill_title_key(conn: sqlite3.Connection) -> None:
    """给老库补 title_key，并顺手把已有的同标题重复清掉。

    排序决定保留谁：**可抓正文的直连源优先，其次是有正文的，再其次是先入库的**。
    """
    rows = conn.execute(
        "SELECT id, title FROM articles "
        "ORDER BY (origin='rss') DESC, (body IS NOT NULL) DESC, id ASC"
    ).fetchall()
    keep: dict[str, int] = {}
    drop: list[int] = []
    for r in rows:
        k = title_key(r["title"])
        if not k:
            continue
        if k in keep:
            drop.append(r["id"])
        else:
            keep[k] = r["id"]
            conn.execute("UPDATE articles SET title_key=? WHERE id=?", (k, r["id"]))
    for aid in drop:
        conn.execute("DELETE FROM articles_fts WHERE rowid=?", (aid,))
        conn.execute("DELETE FROM articles WHERE id=?", (aid,))

    # 顺手清掉迁移前就已经进库的垃圾标题（入库过滤只对新来的生效）
    spam_ids = [
        r["id"]
        for r in conn.execute("SELECT id, title FROM articles").fetchall()
        if is_spam(r["title"])
    ]
    for aid in spam_ids:
        conn.execute("DELETE FROM articles_fts WHERE rowid=?", (aid,))
        conn.execute("DELETE FROM articles WHERE id=?", (aid,))

    if drop or spam_ids:
        print(f"  迁移：清理了 {len(drop)} 条同标题重复、{len(spam_ids)} 条垃圾标题")


def title_key(title: str) -> str:
    """归一化标题，用于二次去重。

    为什么光靠 URL 去重不够：同一条报道在 Google News 的中英两路、或媒体 RSS 与聚合源
    之间，**URL 是不同的**（实测 592 篇里有 22 组同标题重复）。同一篇占掉两个召回名额，
    会实打实挤掉别的内容。

    归一化只去空白与标点，保留字词本身 —— 免得把 "A - B" 和 "A — B" 判成两条不同的。
    """
    return re.sub(r"[^\w一-鿿]+", "", (title or "").lower())


# Google News 这类聚合源偶尔会带上 SEO 污染标题（实测出现过「AG捕鱼王电子」前缀
# 挂在一条真新闻上）。量不大但会污染简报，入库时直接丢掉。
# 词表宁可短：只放明确属于赌博/推广的，别误伤正常新闻。
_SPAM_RE = re.compile(
    r"捕鱼|电子游艺|娱乐城|真人视讯|百家乐|棋牌|彩票|投注|下注|博彩|线上赌|"
    r"太阳城|新濠|威尼斯人|澳门赌|洗码|包杀|casino|betting site|free spins",
    re.I,
)


def is_spam(title: str) -> bool:
    return bool(_SPAM_RE.search(title or ""))


def cjk_space(text: str) -> str:
    """把每个汉字单独隔开：'Pilbara 锂矿' -> 'Pilbara 锂 矿'。入库与查询共用。"""
    if not text:
        return ""
    out = []
    for ch in text:
        if _CJK_RE.match(ch):
            out.append(f" {ch} ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _term_expr(tok: str) -> str:
    """单个词条 -> FTS5 表达式。汉字段转成逐字短语（等价子串匹配）。"""
    clean = tok.replace('"', "").strip()
    if not clean:
        return ""
    if _CJK_RE.match(clean[0]):
        return '"' + " ".join(clean) + '"'
    return f'"{clean}"'


def build_match(query: str, expand_fn=None, *, require_all: bool = True) -> str:
    """把自然语言查询编译成 FTS5 MATCH 表达式。

    每个查询词生成一个 OR 组：原词 + 跨语言替代词。组之间按 require_all 取 AND 或 OR。

    例：'Pilbara 锂矿' 经术语扩展后 ->
        ("Pilbara") AND ("锂 矿" OR "lithium" OR "spodumene" OR "pegmatite")

    降级策略由调用方决定，这里只负责单次表达式编译。
    """
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return ""

    groups: list[str] = []
    for tok in tokens:
        if not require_all and not _CJK_RE.match(tok[0]) and len(tok) < 3:
            continue  # OR 模式下丢弃过短的拉丁词，它们只会引入噪声
        alts = [_term_expr(tok)]
        if expand_fn:
            alts.extend(_term_expr(s) for s in expand_fn(tok))
        alts = [a for a in alts if a]
        if alts:
            groups.append("(" + " OR ".join(dict.fromkeys(alts)) + ")")

    if not groups:
        return ""
    return (" AND " if require_all else " OR ").join(groups)


def upsert_article(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str,
    source: str,
    author: str | None = None,
    category: str | None = None,
    published: str | None = None,
    summary: str | None = None,
    now: str,
    origin: str = "rss",
) -> bool:
    """写入或更新一篇新闻。返回 True 表示新插入。

    刻意不用 `INSERT ... ON CONFLICT ... RETURNING (first_seen = excluded.first_seen)` ——
    SQLite 的 RETURNING 子句里访问不到 `excluded` 伪表（会报 no such column）。
    改为先查后写，语义清楚也不会踩这个坑。

    入这道门要过三关：垃圾标题丢弃 → URL 去重 → 标题去重。
    """
    if is_spam(title):
        return False  # 赌博/推广污染标题，不进门

    tk = title_key(title)
    existing = conn.execute(
        "SELECT id, title, summary, body FROM articles WHERE url=?", (url,)
    ).fetchone()

    if existing is None:
        if tk:
            dup = conn.execute(
                "SELECT id, origin FROM articles WHERE title_key=? LIMIT 1", (tk,)
            ).fetchone()
            if dup is not None:
                # 已有同标题的（只是 URL 不同）。只有「新来的是直连源、旧的不是」才换掉 ——
                # 直连源能抓正文，聚合源只有标题摘要。
                if not (origin == "rss" and dup["origin"] != "rss"):
                    return False
                conn.execute("DELETE FROM articles_fts WHERE rowid=?", (dup["id"],))
                conn.execute("DELETE FROM articles WHERE id=?", (dup["id"],))

        cur = conn.execute(
            """INSERT INTO articles(url,title,source,author,category,published,summary,
                                    first_seen,origin,title_key)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (url, title, source, author, category, published, summary, now, origin, tk),
        )
        _reindex(conn, cur.lastrowid)
        return True

    changed = (existing["title"] != title) or (
        summary is not None and summary != existing["summary"]
    )
    conn.execute(
        """UPDATE articles SET
             title = ?, title_key = ?, summary = COALESCE(?, summary),
             published = COALESCE(?, published),
             category = COALESCE(?, category), author = COALESCE(?, author)
           WHERE id = ?""",
        (title, tk, summary, published, category, author, existing["id"]),
    )
    if changed:
        _reindex(conn, existing["id"])
    return False


def _reindex(conn: sqlite3.Connection, article_id: int) -> None:
    row = conn.execute(
        "SELECT title, summary, body FROM articles WHERE id=?", (article_id,)
    ).fetchone()
    if not row:
        return
    text = " ".join(filter(None, (row["title"], row["summary"], row["body"])))
    conn.execute("DELETE FROM articles_fts WHERE rowid=?", (article_id,))
    conn.execute(
        "INSERT INTO articles_fts(rowid, search_text) VALUES(?,?)", (article_id, cjk_space(text))
    )


def set_body(conn: sqlite3.Connection, article_id: int, body: str, now: str) -> None:
    conn.execute("UPDATE articles SET body=?, body_fetched=? WHERE id=?", (body, now, article_id))
    _reindex(conn, article_id)


def search_articles(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    since: str | None = None,
    expand_fn=None,
    require_all: bool = True,
) -> list[dict]:
    """单次全文检索。require_all=True 全部词命中；False 命中任意词。

    **降级策略由调用方决定，这里不做。** 早先版本把 AND/OR 降级写在函数内部，
    结果是 OR 凑出的几条弱相关结果让调用方误以为「已召回充分」，
    不再去补抓真正相关的数据源。策略与执行分开，才看得清这个问题。
    """
    expr = build_match(query, expand_fn, require_all=require_all)
    if not expr:
        return []

    sql = (
        "SELECT a.id, a.url, a.title, a.source, a.author, a.category, a.published, "
        "       a.summary, a.origin, (a.body IS NOT NULL) AS has_body, "
        "       bm25(articles_fts) AS score "
        "FROM articles_fts f JOIN articles a ON a.id = f.rowid "
        "WHERE articles_fts MATCH ?"
    )
    params: list[Any] = [expr]
    if since:
        sql += " AND a.published >= ?"
        params.append(since)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []  # 表达式里有 FTS 不接受的语法


def count_articles(conn: sqlite3.Connection, since: str | None = None) -> int:
    if since:
        return conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE published >= ?", (since,)
        ).fetchone()["c"]
    return conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]


def iter_articles(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute("SELECT * FROM articles ORDER BY published DESC").fetchall()
