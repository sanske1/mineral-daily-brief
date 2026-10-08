"""lme-price-mcp —— 金属与矿石价格行情 MCP server。

工具：get_price / get_trend / list_commodities / list_sources

四条源，按权威性排序使用：

  ┌─ 上期所官方（沪铜/沪锌/沪镍）── 权威，公开免费，**支持历史**
  ├─ 广期所官方（碳酸锂）──────── 权威，公开免费，**仅最新价**
  ├─ 东方财富（LME / 铁矿石）──── 转载，有历史序列，但会限流
  └─ 新浪财经 ─────────────────── 转载，东财的备份

⚠️ 题目把锂写成「SHFE 锂」有误 —— 碳酸锂期货在**广州期货交易所（GFEX）**，
   上期所没有锂品种。品种表里保留 "SHFE 锂" 作别名，照题目原话提问也能命中。

实测的三个坑（都已在代码里处理）：
  1. 东财实时接口对个别品种返回 `data: null`（如 109.LCPT）→ 必须有日线末值兜底
  2. 东财接口间歇性断连和限流（实测整个 IP 被限到 code=000）→ 快速失败 + 缓存 + 第二源
  3. **广期所官方接口忽略 trade_date，永远返回最新数据** → 只能用于最新价，
     拿它拼历史会得到一条首尾相同的假水平线（详见 _official_series 注释）
"""

from __future__ import annotations

import json
import re
from datetime import date as _date
from datetime import datetime, timedelta, timezone
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
    name="lme-price",
    instructions="金属与矿石价格行情：LME 铜锌镍、上期所沪铜沪锌沪镍、广期所碳酸锂、大商所铁矿石。",
)

# ===========================================================================
# 品种注册表：把用户嘴里的名字（中/英/代码）映射到数据源的真实标识
# ===========================================================================

COMMODITIES: dict[str, dict] = {
    "lme_copper": {
        "name_zh": "LME 铜",
        "name_en": "LME Copper",
        "exchange": "LME",
        "secid": "109.LCPT",  # 综合铜03（3 个月滚动主力）
        "unit": "USD/t",
        "currency": "USD",
        "aliases": ["lme铜", "lme 铜", "伦铜", "铜", "copper", "lme copper", "cad", "lme铜3m"],
    },
    "lme_zinc": {
        "name_zh": "LME 锌",
        "name_en": "LME Zinc",
        "exchange": "LME",
        "secid": "109.LZNT",
        "unit": "USD/t",
        "currency": "USD",
        "aliases": ["lme锌", "lme 锌", "伦锌", "锌", "zinc", "lme zinc", "lznt"],
    },
    "lme_nickel": {
        "name_zh": "LME 镍",
        "name_en": "LME Nickel",
        "exchange": "LME",
        "secid": "109.LNKT",
        "unit": "USD/t",
        "currency": "USD",
        "aliases": ["lme镍", "lme 镍", "伦镍", "镍", "nickel", "lme nickel", "lnkt"],
    },
    "gfex_lithium_carbonate": {
        "name_zh": "碳酸锂",
        "name_en": "Lithium Carbonate",
        "exchange": "GFEX",
        "secid": "225.lc2610",
        "unit": "CNY/t",
        "currency": "CNY",
        "rollable": ["225.lc{yy}{mm}"],  # 主力合约换月时按月份向后试探
        "aliases": ["碳酸锂", "锂", "锂价", "shfe锂", "shfe 锂", "lithium", "lithium carbonate", "lc", "锂盐"],
    },
    "dce_iron_ore": {
        "name_zh": "铁矿石",
        "name_en": "Iron Ore",
        "exchange": "DCE",
        "secid": "114.i2610",
        "unit": "CNY/t",
        "currency": "CNY",
        "rollable": ["114.i{yy}{mm}"],
        "aliases": ["铁矿石", "铁矿", "铁", "iron ore", "ironore", "i", "普氏"],
    },
    "shfe_nickel": {
        "name_zh": "沪镍",
        "name_en": "SHFE Nickel",
        "exchange": "SHFE",
        "secid": "113.ni2610",
        "unit": "CNY/t",
        "currency": "CNY",
        "rollable": ["113.ni{yy}{mm}"],
        "aliases": ["沪镍", "沪 镍", "ni", "shfe镍"],
    },
    "shfe_copper": {
        "name_zh": "沪铜",
        "name_en": "SHFE Copper",
        "exchange": "SHFE",
        "secid": "113.cu2610",
        "unit": "CNY/t",
        "currency": "CNY",
        "rollable": ["113.cu{yy}{mm}"],
        "aliases": ["沪铜", "沪 铜", "cu", "shfe铜"],
    },
    "shfe_zinc": {
        "name_zh": "沪锌",
        "name_en": "SHFE Zinc",
        "exchange": "SHFE",
        "secid": "113.zn2610",
        "unit": "CNY/t",
        "currency": "CNY",
        "rollable": ["113.zn{yy}{mm}"],
        "aliases": ["沪锌", "沪 锌", "zn"],
    },
}

# 官方直连源标识。
# ⚠️ history 字段是**实测出来的**，不是猜的：
#   · SHFE 的 URL 里带日期，取 20260825/20260901/20260915 返回三个不同价格
#     （且主力按月末换月 2610 -> 2611），说明它真的按日期取历史。
#   · GFEX 的 trade_date 参数**被忽略**：取 20260801/20260901/20260930/20261008
#     返回的是完全相同的最新数据。所以它只能用于「最新价」。
OFFICIAL_CODES: dict[str, dict] = {
    "shfe_copper": {"source": "shfe_official", "group": "cu", "name": "沪铜", "history": True},
    "shfe_nickel": {"source": "shfe_official", "group": "ni", "name": "沪镍", "history": True},
    "shfe_zinc": {"source": "shfe_official", "group": "zn", "name": "沪锌", "history": True},
    "gfex_lithium_carbonate": {
        "source": "gfex_official",
        "group": "lc",
        "name": "碳酸锂",
        "history": False,
    },
}

# 备用价格源：新浪财经行情代码。hf_* 是外盘（LME），nf_* 是内盘期货。
# 注意：这个接口返回 GBK，按 UTF-8 解会把中文名解成乱码。
SINA_CODES: dict[str, str] = {
    "lme_copper": "hf_CAD",
    "lme_zinc": "hf_ZSD",
    "lme_nickel": "hf_NID",
    "gfex_lithium_carbonate": "nf_LC0",
    "dce_iron_ore": "nf_I0",
    "shfe_nickel": "nf_NI0",
    "shfe_copper": "nf_CU0",
    "shfe_zinc": "nf_ZN0",
}

_ALIAS_INDEX: dict[str, str] = {}
for _key, _meta in COMMODITIES.items():
    _ALIAS_INDEX[_key.lower()] = _key
    _ALIAS_INDEX[_meta["name_zh"].lower()] = _key
    _ALIAS_INDEX[_meta["name_en"].lower()] = _key
    for _a in _meta["aliases"]:
        _ALIAS_INDEX.setdefault(_a.lower(), _key)


def resolve(name: str) -> str | None:
    """把任意写法解析成规范品种名；认不出返回 None（调用方负责报错，不瞎猜）。"""
    if not name:
        return None
    key = name.strip().lower()
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    squeezed = key.replace(" ", "")
    for alias, canon in _ALIAS_INDEX.items():
        if alias.replace(" ", "") == squeezed:
            return canon
    # 最后尝试子串：查询里带了额外修饰词，如 "今天的碳酸锂价格"
    for alias, canon in sorted(_ALIAS_INDEX.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 2 and alias in key:
            return canon
    return None


def known_names() -> list[str]:
    return [f"{m['name_zh']} ({k})" for k, m in COMMODITIES.items()]


def price_scale(secid: str) -> float:
    """东财对国际盘返回的是整数化的报价，需要按品种缩放回真实价格。

    实测：LME 综合铜03 的 f43=14488 对应 14,488 USD/t（1:1）；
          沪镍 f43=118430 对应 118,430 CNY/t（1:1）；
          铁矿石 f43=6835 对应 683.5 CNY/t（1:10，报价精确到 0.1 元）。
    """
    code = secid.split(".", 1)[-1].lower()
    if code.startswith("i"):
        return 10.0
    return 1.0


# ===========================================================================
# 东方财富（转载源）
# ===========================================================================

QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 东财接口要求带 Referer，否则部分节点会拒
_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

SINA_URL = "https://hq.sinajs.cn/list="
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn/"}
_SINA_RE = re.compile(r'var hq_str_(\w+)="([^"]*)"')

SHFE_DAY_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat"
GFEX_DAY_URL = "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
_GFEX_HEADERS = {"Referer": "http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml"}

_FOREVER = 10 * 365 * 24 * 3600.0
_MONTH_RE = re.compile(r"\d{4}")  # 交割月形如 2610；汇总行是「小计」


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(data: dict, key: str) -> float | None:
    v = data.get(key)
    if v in (None, "-", ""):
        return None
    return _f(v)


def _scale(secid: str, decimals: int | None) -> float:
    """优先用返回体里的 f59（小数位数），缺失时退回品种表里的经验值。"""
    if isinstance(decimals, int) and decimals > 0:
        return float(10**decimals)
    return price_scale(secid)


def _tier_fields(source_key: str) -> dict[str, Any]:
    return cache.tier_fields(source_key)


def _quote(secid: str) -> tuple[dict[str, Any] | None, str | None]:
    """东财实时报价。返回 (报价, 陈旧提示)。None 表示该 secid 实时接口无数据。"""
    url = f"{QUOTE_URL}?secid={secid}&fields=f43,f44,f45,f46,f57,f58,f59,f60,f86,f169,f170,f168"
    try:
        payload, meta = cache.fetch_json(
            url, headers=_HEADERS, ttl=120, timeout=8, retries=1, allow_stale_on_error=True
        )
    except cache.FetchError:
        return None, None

    data = payload.get("data")
    if not data:
        return None, None

    div = _scale(secid, data.get("f59"))
    last = _num(data, "f43")
    if last is None:
        return None, None

    ts = _num(data, "f86")
    quote = {
        "code": data.get("f57"),
        "name": data.get("f58"),
        "last": round(last / div, 6),
        "open": round(_num(data, "f46") / div, 6) if _num(data, "f46") is not None else None,
        "high": round(_num(data, "f44") / div, 6) if _num(data, "f44") is not None else None,
        "low": round(_num(data, "f45") / div, 6) if _num(data, "f45") is not None else None,
        "prev_close": round(_num(data, "f60") / div, 6) if _num(data, "f60") is not None else None,
        "change": round(_num(data, "f169") / div, 6) if _num(data, "f169") is not None else None,
        "change_pct": _num(data, "f170"),
        "as_of": (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None),
        "source": "东方财富 push2 实时报价",
    }
    return quote, cache.stale_note(meta)


def _klines(secid: str, key: str, limit: int = 60) -> tuple[list[dict[str, Any]], str | None]:
    """东财日线，失败自动换新浪兜底。返回 (序列, 提示语)。

    三个设计点，都是被真实故障逼出来的：

    1. **固定请求长度**（恒取 `lmt=300`，本地切片）。早先按需要的长度请求，
       于是 lmt=30 和 lmt=45 是两个不同的 URL、两个不同的缓存项 ——
       同一份数据反复回源，既慢又更容易撞上限流。固定长度后缓存才真正命中。
    2. **失败不外抛**：东财日线会间歇性断连、也会限流（实测整个 IP 被限到 code=000）。
       取不到就当正常分支返回空，由调用方如实告知。
    3. **新浪兜底**：东财整体不可用时自动换新浪的历史接口，并标明来源。
    """
    url = (
        f"{KLINE_URL}?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt=101&fqt=1&end=20500101&lmt=300"
    )
    note: str | None = None
    try:
        payload, meta = cache.fetch_json(
            url, headers=_HEADERS, ttl=1800, timeout=8, retries=1, allow_stale_on_error=True
        )
        note = cache.stale_note(meta)
    except cache.FetchError:
        payload = None

    data = ((payload or {}).get("data") or {}) if payload else {}
    if not data.get("klines"):
        rows = _sina_klines(key)
        if rows:
            return rows[-limit:], "东方财富日线不可用，已改用新浪财经历史数据"
        return [], note

    out: list[dict[str, Any]] = []
    for row in data.get("klines") or []:
        parts = row.split(",")
        if len(parts) < 5:
            continue

        def g(i: int) -> float | None:
            try:
                return float(parts[i])
            except (ValueError, IndexError):
                return None

        out.append(
            {
                "date": parts[0],
                "open": g(1),
                "close": g(2),
                "high": g(3),
                "low": g(4),
                "volume": g(5),
            }
        )
    return out[-limit:], note


# ===========================================================================
# 新浪财经（转载源，东财的备份）
# ===========================================================================


def _sina_quote(key: str) -> dict[str, Any] | None:
    """新浪实时报价。解析两种格式（实测字段位置）：

      hf_*（外盘）: 0现价 4最高 5最低 7昨收 12日期 13名称
      nf_*（内盘）: 0名称 2开盘 3最高 4最低 8最新 9昨结算

    这个接口返回 **GBK**，按 UTF-8 解码会把中文名变成乱码。
    """
    code = SINA_CODES.get(key)
    if not code:
        return None
    try:
        text, _ = cache.fetch_text(
            f"{SINA_URL}{code}",
            headers=_SINA_HEADERS,
            encoding="gbk",
            ttl=120,
            timeout=8,
            retries=1,
            allow_stale_on_error=True,
        )
    except cache.FetchError:
        return None

    m = _SINA_RE.search(text)
    if not m:
        return None
    parts = m.group(2).split(",")
    if len(parts) < 9:
        return None

    def g(i: int) -> float | None:
        try:
            v = float(parts[i])
            return v if v != 0 else None
        except (ValueError, IndexError):
            return None

    if code.startswith("hf_"):
        last, high, low, prev = g(0), g(4), g(5), g(7)
        name = parts[13] if len(parts) > 13 else ""
        day = parts[12] if len(parts) > 12 else None
    else:
        last, high, low, prev = g(8), g(3), g(4), g(9)
        name = parts[0]
        day = None

    if last is None:
        return None

    change = round(last - prev, 6) if prev else None
    return {
        "name": name,
        "last": last,
        "high": high,
        "low": low,
        "prev_close": prev,
        "change": change,
        "change_pct": round(change / prev * 100, 3) if (change is not None and prev) else None,
        "date": day,
        "source": f"新浪财经 hq.sinajs.cn ({code})",
    }


_SINA_KLINE_INNER = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp.php/x/"
    "InnerFuturesNewService.getDailyKLine?symbol={sym}"
)
_SINA_KLINE_GLOBAL = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp.php/x/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}"
)


def _sina_klines(key: str) -> list[dict[str, Any]]:
    """新浪期货日 K。内外盘两个接口，字段名不同。

    返回体是 JSONP（`/*<script>...*/ x([...])`），要先把 `[...]` 抠出来再解析。
    新浪一次返回**全部历史**（LME 铜可到 2016 年），所以不需要翻页。
    """
    code = SINA_CODES.get(key)
    if not code:
        return []
    is_inner = code.startswith("nf_")
    sym = code.split("_", 1)[1]
    url = (_SINA_KLINE_INNER if is_inner else _SINA_KLINE_GLOBAL).format(sym=sym)

    try:
        text, _ = cache.fetch_text(
            url,
            headers=_SINA_HEADERS,
            encoding="utf-8",
            ttl=1800,
            timeout=10,
            retries=1,
            allow_stale_on_error=True,
        )
    except cache.FetchError:
        return []

    lo, hi = text.find("["), text.rfind("]")
    if lo < 0 or hi < 0:
        return []
    try:
        arr = json.loads(text[lo: hi + 1])
    except (json.JSONDecodeError, TypeError):
        return []

    out: list[dict[str, Any]] = []
    for r in arr:
        if is_inner:
            d, o, h, lw, c, v = (r.get(k) for k in ("d", "o", "h", "l", "c", "v"))
        else:
            d, o, h, lw, c, v = (
                r.get(k) for k in ("date", "open", "high", "low", "close", "volume")
            )
        if not d:
            continue
        out.append(
            {"date": d, "open": _f(o), "close": _f(c), "high": _f(h), "low": _f(lw), "volume": _f(v)}
        )
    return out


# ===========================================================================
# 官方直连源
#
# 上期所与广期所的日行情都是**公开免费**的，不需要登录也不需要 key。
# 既然是权威源，就不该退而求其次用门户转载 —— 所以这两个交易所的品种优先走官方。
#
# 关键设计：**历史日行情文件是不可变的**。2026-10-08 收盘后那个文件的内容
# 永远不会再变。所以历史日期缓存 10 年（等于永久），今天用短 TTL。
# 这让 get_trend(30天) 的代价变成「首次约 20 次请求，之后全免费」。
# ===========================================================================


def _cache_ttl_for(day: _date) -> float:
    return 300.0 if day >= datetime.now(timezone.utc).date() else _FOREVER


def _shfe_day(day: _date) -> list[dict[str, Any]]:
    """上期所某交易日的全部合约行情。非交易日返回空列表（上游 404，属正常）。"""
    url = SHFE_DAY_URL.format(date=day.strftime("%Y%m%d"))
    try:
        payload, _ = cache.fetch_json(
            url, headers=_HEADERS, ttl=_cache_ttl_for(day), timeout=15,
            retries=1, allow_stale_on_error=True,
        )
    except cache.FetchError:
        return []  # 周末/节假日会 404，这不算错误
    rows = payload.get("o_curinstrument") or []
    return [r for r in rows if isinstance(r, dict)]


def _gfex_day(day: _date) -> list[dict[str, Any]]:
    """广期所某交易日的全部合约行情。trade_type=0 是期货（1 是期权）。"""
    try:
        payload, _ = cache.post_form_json(
            GFEX_DAY_URL,
            {"trade_date": day.strftime("%Y%m%d"), "trade_type": "0"},
            headers=_GFEX_HEADERS,
            ttl=_cache_ttl_for(day),
            timeout=15,
            retries=1,
            allow_stale_on_error=True,
        )
    except cache.FetchError:
        return []
    if str(payload.get("code")) != "0":
        return []
    return [r for r in (payload.get("data") or []) if isinstance(r, dict)]


def _shfe_quote(key: str, meta: dict, day: _date) -> dict[str, Any] | None:
    off = OFFICIAL_CODES[key]
    group = off["group"]
    # 注意：上期所返回里 **PRODUCTID 是品种组 id（如 ni_f），不是合约号**，
    # 真正区分合约的是 DELIVERYMONTH（2610/2611…）。
    # 每组最后还有一行汇总，DELIVERYMONTH 是「小计」，收盘价为空 ——
    # 不过滤掉的话它会因为持仓量最大而被当成「主力合约」，取到空价格。
    rows = [
        r
        for r in _shfe_day(day)
        if str(r.get("PRODUCTGROUPID", "")).lower() == group
        and _MONTH_RE.fullmatch(str(r.get("DELIVERYMONTH", "")))
    ]
    if not rows:
        return None

    main = max(rows, key=lambda r: _f(r.get("OPENINTEREST")) or 0.0)  # 主力=持仓量最大
    close = _f(main.get("CLOSEPRICE"))
    if close is None:
        return None
    prev = _f(main.get("PRESETTLEMENTPRICE")) or _f(main.get("SETTLEMENTPRICE"))
    change = round(close - prev, 6) if prev else None

    return {
        "name": off["name"],
        "contract": f"{group}{main.get('DELIVERYMONTH')}",
        "last": close,
        "open": _f(main.get("OPENPRICE")),
        "high": _f(main.get("HIGHESTPRICE")),
        "low": _f(main.get("LOWESTPRICE")),
        "settlement": _f(main.get("SETTLEMENTPRICE")),
        "prev_close": prev,
        "change": change,
        "change_pct": round(change / prev * 100, 3) if (change is not None and prev) else None,
        "volume": _f(main.get("VOLUME")),
        "open_interest": _f(main.get("OPENINTEREST")),
        "date": day.isoformat(),
        "contracts_available": len(rows),
        "source_key": "shfe_official",
    }


def _gfex_quote(key: str, meta: dict, day: _date) -> dict[str, Any] | None:
    off = OFFICIAL_CODES[key]
    group = off["group"]
    rows = [
        r
        for r in _gfex_day(day)
        # 小计/总计行的 varietyOrder 不是品种代码，delivMonth 也非合约月份，滤掉
        if str(r.get("varietyOrder", "")).lower() == group and r.get("delivMonth")
    ]
    if not rows:
        return None

    main = max(rows, key=lambda r: _f(r.get("openInterest")) or 0.0)
    close = _f(main.get("close"))
    if close is None:
        return None
    prev = _f(main.get("lastClear"))
    change = round(close - prev, 6) if prev else None

    return {
        "name": off["name"],
        "contract": f"{group}{main.get('delivMonth')}",
        "last": close,
        "open": _f(main.get("open")),
        "high": _f(main.get("high")),
        "low": _f(main.get("low")),
        "settlement": _f(main.get("clearPrice")),
        "prev_close": prev,
        "change": change,
        "change_pct": round(change / prev * 100, 3) if (change is not None and prev) else None,
        "volume": _f(main.get("volumn")),
        "open_interest": _f(main.get("openInterest")),
        "date": day.isoformat(),
        "contracts_available": len(rows),
        "source_key": "gfex_official",
    }


def _official_quote(key: str, meta: dict, day: _date) -> dict[str, Any] | None:
    """有官方源的品种走官方；没有（LME / 铁矿石）返回 None，由调用方走转载源。"""
    off = OFFICIAL_CODES.get(key)
    if not off:
        return None
    if off["source"] == "shfe_official":
        return _shfe_quote(key, meta, day)
    if off["source"] == "gfex_official":
        return _gfex_quote(key, meta, day)
    return None


def _official_series(key: str, meta: dict, days: int) -> tuple[list[dict[str, Any]], str | None]:
    """逐日取官方行情拼成序列。单日失败不影响整体，缺的那天跳过。

    **只对 history=True 的源可用**。广期所的接口忽略 trade_date、永远返回最新数据，
    拿它拼序列会得到一条首末相同的假水平线 —— 那比没有数据更糟，因为它看起来像真的。
    """
    off = OFFICIAL_CODES.get(key) or {}
    if not off.get("history"):
        return [], None
    end = datetime.now(timezone.utc).date()
    out: list[dict[str, Any]] = []
    misses = 0
    # 从 end 往前取自然日（含周末，官方接口对非交易日返回空，会被跳过）
    for day in reversed([end - timedelta(days=i) for i in range(days)]):
        q = _official_quote(key, meta, day)
        if q:
            out.append(
                {
                    "date": q["date"],
                    "open": q["open"],
                    "close": q["last"],
                    "high": q["high"],
                    "low": q["low"],
                    "volume": q["volume"],
                }
            )
        else:
            misses += 1
    if not out:
        return [], None
    note = None
    if misses:
        note = f"官方源逐日取数，其中 {misses} 个自然日无数据（非交易日或尚未发布），已跳过"
    return out, note


# ===========================================================================
# 辅助
# ===========================================================================


def _resolve(commodity: str) -> tuple[str, dict] | dict:
    """把用户写的品种名解析成 (规范名, 元数据)；认不出就返回错误体。"""
    key = resolve(commodity)
    if not key:
        return {
            "error": "unknown_commodity",
            "input": commodity,
            "reason": f"认不出品种 {commodity!r}。",
            "known": known_names(),
        }
    return key, COMMODITIES[key]


def _try_rollable(meta: dict, on: _date) -> list[str]:
    """主力合约会换月。按当前月及其后 3 个月生成候选 secid。"""
    out: list[str] = []
    for tmpl in meta.get("rollable", []):
        y, m = on.year, on.month
        for _ in range(4):
            out.append(tmpl.format(yy=str(y)[2:], mm=f"{m:02d}"))
            m += 1
            if m > 12:
                m, y = 1, y + 1
    return out


def _cross_check(east: dict, sina: dict, tol: float = 1.0) -> dict:
    """两个独立价格源交叉校验。

    两源取的可能是不同月份合约（主力连续 vs 具体合约），小幅差异是正常的；
    只在**超过容差**时提示，避免把「合约月份不同」误报成「数据错误」。
    """
    if not east or not sina or not east.get("last") or not sina.get("last"):
        return {"status": "insufficient", "reason": "至少一路源没有报价"}
    a, b = east["last"], sina["last"]
    diff_pct = abs(a - b) / max(abs(a), abs(b)) * 100
    ok = diff_pct <= tol
    return {
        "status": "agree" if ok else "divergent",
        "eastmoney": a,
        "sina": b,
        "diff_pct": round(diff_pct, 3),
        "note": "两源一致" if ok else f"两源差异 {diff_pct:.2f}%，可能是不同月份合约，建议人工核对",
    }


def _official_payload(key: str, meta: dict, oq: dict, note: str | None) -> dict:
    """把官方报价组装成 get_price 的统一返回体。"""
    tier = _tier_fields(oq["source_key"])
    return {
        "commodity": key,
        "name_zh": meta["name_zh"],
        "exchange": meta["exchange"],
        "unit": meta["unit"],
        "currency": meta["currency"],
        "date": oq["date"],
        "price": oq["last"],
        "change_pct": oq["change_pct"],
        "prev_close": oq["prev_close"],
        "day_range": [oq["low"], oq["high"]],
        "open": oq["open"],
        "settlement": oq["settlement"],
        "volume": oq["volume"],
        "open_interest": oq["open_interest"],
        "contract": oq["contract"],
        "contracts_available": oq["contracts_available"],
        "is_realtime": oq["date"] == datetime.now(timezone.utc).date().isoformat(),
        "note": note,
        "source": cache.describe_source(oq["source_key"])["name"],
        **tier,
    }


# ===========================================================================
# 工具
# ===========================================================================


@mcp.tool()
def get_price(
    commodity: Annotated[
        str, Field(description="品种名，中英/代码/别名均可，如 '碳酸锂'、'LME 铜'、'iron ore'")
    ],
    date: Annotated[
        str | None, Field(description="日期 YYYY-MM-DD。不传或传今天取最新价；历史日期取当日收盘")
    ] = None,
    verify: Annotated[
        bool, Field(description="同时查东财与新浪两个源并交叉校验（多一次请求）")
    ] = False,
) -> dict:
    """查询某品种的价格。返回体带 source_tier 标明数据源权威性。"""
    res = _resolve(commodity)
    if isinstance(res, dict):
        return res
    key, meta = res

    want = None
    if date:
        try:
            want = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return {"error": "bad_date", "input": date, "reason": "日期格式应为 YYYY-MM-DD"}

    today = datetime.now(timezone.utc).date()

    # ---- 阶梯 1：官方直连（上期所 / 广期所）----
    # 权威源免费可用时就不该用转载源。只有 LME 与铁矿石没有官方源（官网 403 / 412）。
    if key in OFFICIAL_CODES:
        off_meta = OFFICIAL_CODES[key]
        target = want or today
        # 不支持历史取数的官方源（广期所）**只用于「最新价」**。
        # 否则会把今天的数据当成用户要的那个历史日期返回 —— 日期是错的、数字看着完全正常，
        # 这种错最难被发现。
        official_usable = bool(off_meta.get("history")) or target >= today
        note = None
        oq = _official_quote(key, meta, target) if official_usable else None
        if oq is None and want is not None and official_usable:
            # 指定日期可能是周末/节假日，官方当天没有文件 -> 往前找最近一个有数据的交易日
            for back in range(1, 8):
                oq = _official_quote(key, meta, target - timedelta(days=back))
                if oq:
                    note = f"{target} 官方无当日行情（非交易日），已取最近交易日 {oq['date']}"
                    break
        if oq:
            return _official_payload(key, meta, oq, note)
        # 官方源本次取不到（网络/停市），继续往下走转载源

    # ---- 阶梯 2：转载源实时报价 ----
    if want is None or want >= today:
        q, stale = _quote(meta["secid"])
        if q is None and meta.get("rollable"):
            for cand in _try_rollable(meta, today):
                q, stale = _quote(cand)
                if q:
                    q["note"] = f"配置的主力 {meta['secid']} 无实时数据，已自动换到 {cand}"
                    break

        # 只在东财失败、或调用方明确要求校验时，才多打一次新浪，避免无谓的请求量
        sq = _sina_quote(key) if (q is None or verify) else None
        cross = _cross_check(q, sq) if (verify and q and sq) else None

        if q:
            out = {
                "commodity": key,
                "name_zh": meta["name_zh"],
                "exchange": meta["exchange"],
                "unit": meta["unit"],
                "currency": meta["currency"],
                "date": want.isoformat() if want else today.isoformat(),
                "price": q["last"],
                "change_pct": q["change_pct"],
                "prev_close": q["prev_close"],
                "day_range": [q["low"], q["high"]],
                "as_of": q["as_of"],
                "source": q["source"],
                "is_realtime": True,
                "stale_note": stale,
                **_tier_fields("eastmoney"),
            }
            if cross:
                out["cross_check"] = cross
            return out

        # 东财实时不可用（实测 109.LCPT 常态如此）-> 用新浪兜底
        if sq:
            return {
                "commodity": key,
                "name_zh": meta["name_zh"],
                "exchange": meta["exchange"],
                "unit": meta["unit"],
                "currency": meta["currency"],
                "date": want.isoformat() if want else today.isoformat(),
                "price": sq["last"],
                "change_pct": sq["change_pct"],
                "prev_close": sq["prev_close"],
                "day_range": [sq["low"], sq["high"]],
                "is_realtime": True,
                "note": "东方财富实时接口对该品种无数据，已改用新浪财经备用源",
                "source": sq["source"],
                **_tier_fields("sina_finance"),
            }

        fallback_reason = "两个实时源都不可用，已改用日线收盘价"
    else:
        fallback_reason = None

    # ---- 阶梯 3：日线兜底 ----
    klines, kline_stale = _klines(meta["secid"], key, limit=300)
    if not klines and meta.get("rollable"):
        for cand in _try_rollable(meta, want or today):
            klines, kline_stale = _klines(cand, key, limit=300)
            if klines:
                break

    if not klines:
        return {
            "commodity": key,
            "error": "no_price_data",
            "reason": f"{meta['name_zh']}（{meta['secid']}）既没有实时报价也没有日线数据。",
        }

    if want is None:
        row = klines[-1]
        note = fallback_reason
    else:
        exact = [k for k in klines if k["date"] == want.isoformat()]
        if exact:
            row, note = exact[0], fallback_reason
        else:
            prior = [k for k in klines if k["date"] <= want.isoformat()]
            if not prior:
                return {
                    "commodity": key,
                    "error": "date_out_of_range",
                    "reason": f"{want} 早于可查询的最早日期 {klines[0]['date']}。",
                    "earliest_available": klines[0]["date"],
                }
            row = prior[-1]
            note = f"{want} 非交易日，已取最近的前一个交易日 {row['date']}"

    return {
        "commodity": key,
        "name_zh": meta["name_zh"],
        "exchange": meta["exchange"],
        "unit": meta["unit"],
        "currency": meta["currency"],
        "date": row["date"],
        "price": row["close"],
        "day_range": [row["low"], row["high"]],
        "open": row["open"],
        "volume": row["volume"],
        "is_realtime": False,
        "note": note,
        "stale_note": kline_stale,
        "source": "东方财富 push2his 日线",
        **_tier_fields("eastmoney"),
    }


@mcp.tool()
def get_trend(
    commodity: Annotated[str, Field(description="品种名，中英/代码/别名均可")],
    days: Annotated[int, Field(description="回溯天数", ge=2, le=500)] = 30,
) -> dict:
    """查询某品种近 N 日的价格走势与区间统计。"""
    res = _resolve(commodity)
    if isinstance(res, dict):
        return res
    key, meta = res

    series: list[dict[str, Any]] = []
    source_key = "eastmoney"
    note: str | None = None
    used = meta["secid"]

    # ---- 阶梯 1：官方逐日序列（上期所 / 广期所）----
    # 官方是「一天一个文件」，所以要按**自然日**多取一截再截断，
    # 否则周末和节假日会把交易日名额吃掉，30 天只剩 20 来个交易日。
    if key in OFFICIAL_CODES:
        rows, off_note = _official_series(key, meta, min(days + 15, 120))
        if len(rows) >= 2:
            series = rows
            source_key = OFFICIAL_CODES[key]["source"]
            note = off_note

    # ---- 阶梯 2：转载源日线（LME / 铁矿石，或官方取数失败时）----
    if not series:
        klines, stale = _klines(meta["secid"], key, limit=300)
        if not klines and meta.get("rollable"):
            for cand in _try_rollable(meta, datetime.now(timezone.utc).date()):
                klines, stale = _klines(cand, key, limit=300)
                if klines:
                    used = cand
                    break
        series = klines
        note = stale
        source_key = "eastmoney"

    if not series:
        return {
            "commodity": key,
            "error": "no_price_data",
            "reason": f"{meta['name_zh']} 没有可用的日线数据。",
        }

    series = series[-days:]
    if len(series) < 2:
        return {
            "commodity": key,
            "error": "insufficient_history",
            "reason": f"仅取到 {len(series)} 个交易日，无法算走势。",
            "series": series,
        }

    closes = [r["close"] for r in series if r["close"] is not None]
    first, last = closes[0], closes[-1]
    hi = max(series, key=lambda r: r["high"] or float("-inf"))
    lo = min(series, key=lambda r: r["low"] or float("inf"))
    change = last - first
    change_pct = (change / first * 100) if first else None

    # 波动率：日收益率标准差（简单口径，够用且好解释）
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]
    ]
    mean = sum(rets) / len(rets) if rets else 0.0
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if len(rets) > 1 else 0.0

    return {
        "commodity": key,
        "name_zh": meta["name_zh"],
        "exchange": meta["exchange"],
        "unit": meta["unit"],
        "currency": meta["currency"],
        "days_requested": days,
        "trading_days": len(series),
        "period": [series[0]["date"], series[-1]["date"]],
        "first_close": first,
        "last_close": last,
        "change": round(change, 6),
        "change_pct": round(change_pct, 3) if change_pct is not None else None,
        "high": {"value": hi["high"], "date": hi["date"]},
        "low": {"value": lo["low"], "date": lo["date"]},
        "daily_volatility_pct": round(vol * 100, 3),
        "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
        "series": series,
        "source": cache.describe_source(source_key)["name"],
        "secid_used": used,
        "note": note,
        **_tier_fields(source_key),
    }


@mcp.tool()
def list_commodities() -> dict:
    """列出所有可查询的品种及其别名。"""
    return {
        "count": len(COMMODITIES),
        "commodities": [
            {
                "key": k,
                "name_zh": v["name_zh"],
                "name_en": v["name_en"],
                "exchange": v["exchange"],
                "unit": v["unit"],
                "aliases": v["aliases"],
            }
            for k, v in COMMODITIES.items()
        ],
    }


@mcp.tool()
def list_sources() -> dict:
    """列出所有上游数据源及其权威性等级，含实测拿不到的那些。"""
    by_tier: dict[str, list[dict]] = {}
    for src_key in cache.SOURCES:
        info = cache.describe_source(src_key)
        if not info.get("available", True):
            continue  # 拿不到的单独列在 unavailable，不混进在用清单里
        by_tier.setdefault(info["tier_label"], []).append(
            {"key": src_key, "name": info["name"], "note": info["note"]}
        )
    return {
        "tier_meaning": {
            "官方直连": "交易所/公司官网一手数据，可直接引用",
            "转载源": "门户转载的官方行情，数值通常一致但可能滞后",
            "替代口径": "用相近但不同的东西顶替，不等价，引用时须说明",
        },
        "by_tier": by_tier,
        "unavailable": cache.unavailable_sources(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
