"""mineral-pdf-mcp —— 从 NI 43-101 / JORC 技术报告 PDF 里抽储量表。

工具：extract_resources / known_reports

⚠️ 题目把 Pilbara 归为 NI 43-101 有误：Pilbara Minerals 是澳交所上市公司，
   报的是 **JORC**。两者表格结构一致（Measured/Indicated/Inferred 三分类 +
   吨位/品位/金属量），所以用同一套解析器兼容，不因此漏掉数据。

解析路线 —— 为什么要自己按坐标重建，而不用 pdfplumber 的表格检测：

  实测两种真实报告，默认方法都不行：
    · Implats AIF（无框线）  `extract_tables()` 返回 **0 张表**
    · Pilbara 年报（有框线） 检测到表，但**标签列被丢掉**，只剩一串数字

  改用的办法是**数据驱动定列**：
    1. 把词按纵坐标聚成行，找出含 Tonnes+Grade 的表头行
    2. 只扫「含分类词且有数字」的行作为数据行
    3. 把这些行里**数字的横坐标聚类成列**（列的位置由数据本身决定，不猜）
    4. 对每个数据列，取横向邻近的表头词判定语义（吨位/品位/金属量）与单位

  第 3 步是关键：真实报告里表头词挨得极近（Pilbara 相邻列中心只差 40pt），
  按表头词间距合并列会全部糊成一团；按数据数字的横坐标分列则天然分开。

宁可 abstain 也不编：找不到表就返回 status=no_table_found 和原因，绝不拿正文段落
里的数字拼一张假表。
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

# ---- 静音 FastMCP 的启动日志 ----
# FastMCP 默认会往 stderr 打一堆启动日志和 rich 横幅。它**走 stderr**，所以不会污染
# stdio 协议；但会把 CLI 输出搞得很吵。这三个开关必须在 import fastmcp **之前**生效。
import os as _os_for_logging

_os_for_logging.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
_os_for_logging.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
_os_for_logging.environ.setdefault("FASTMCP_ENABLE_RICH_LOGGING", "false")

import pdfplumber
from fastmcp import FastMCP
from pydantic import Field

import cache

mcp = FastMCP(
    name="mineral-pdf",
    instructions="从 NI 43-101 / JORC 矿产技术报告 PDF 中抽取 Indicated/Inferred 储量表。",
)

CATEGORIES = {
    "measured": "Measured",
    "indicated": "Indicated",
    "inferred": "Inferred",
    "proved": "Proved",
    "proven": "Proved",
    "probable": "Probable",
    "探明": "Measured",
    "控制": "Indicated",
    "推断": "Inferred",
}
SUMMARY_LABELS = ("sub total", "subtotal", "total", "合计", "小计")

# 「没有数据」的占位符（含 U+2011 非断字连字符）
NULL_TOKENS = {"-", "–", "—", "‑", "n/a", "na", "nil", ""}
NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")

# 表头词 -> 语义。顺序即优先级，先匹配到的胜出。
_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tonnes", ("tonnes", "tonnage", "mt", "矿石量", "吨位", "quantity")),
    ("grade", ("grade", "品位", "g/t", "li2o", "lio", "fe2o3", "ta2o5", "%", "ppm", "gt ")),
    ("contained", ("contained", "metal", "moz", "koz", "kt", "金属量", "含量")),
)
_HEADER_UNIT_RE = re.compile(r"\((mt|g/t|moz|koz|kt|ppm|t|oz|%|矿石量|金属量)[^)]*\)", re.I)
_ANY_PAREN_RE = re.compile(r"\(([^)]*)\)")
# 交割月/合约月形如 2610；汇总行的对应字段是「小计」，用它把汇总行滤掉
_MONTH_RE = re.compile(r"\d{4}")

# 手工维护的已知储量报告索引。
#
# ⚠️ 这是**人工登记的清单，不是自动发现**。自动找到「某公司最新的技术报告 PDF」
#    需要爬交易所公告系统（SEDAR+ / ASX），那是另一个量级的工程，也不稳定。
#    这里只登记已验证可下载、且能正确解析出储量表的报告，宁可少而准。
KNOWN_REPORTS: list[dict[str, str]] = [
    {
        "company": "Pilbara Minerals",
        "aliases": "pilbara,pilgangoora,pls,皮尔巴拉",
        "project": "Pilgangoora",
        "standard": "JORC",
        "url": "https://www.pls.com/documents/pls-annual-report-2026-260821-final-(linked).pdf",
        "note": "Pilbara Minerals 2026 年报，第 33 页起为 Pilgangoora 资源量与储量表（JORC）",
    },
    {
        "company": "Impala Platinum (Implats)",
        "aliases": "implats,impala,英帕拉",
        "project": "Impala Bafokeng / UG2 / Merensky",
        "standard": "NI 43-101 式（AIF）",
        "url": "https://s29.q4cdn.com/841442677/files/doc_downloads/2024/2023-annual-information-form.pdf",
        "note": "2023 年度信息表，第 102 页为 Impala Bafokeng 矿产资源声明（含 Indicated/Inferred）",
    },
]


# ===========================================================================
# 基础工具
# ===========================================================================


def _norm_num(tok: str) -> float | None:
    """单元格文本 -> 数字；「无数据」占位符返回 None。"""
    t = (tok or "").replace("\xa0", " ").strip().replace(" ", "")
    if t.lower() in NULL_TOKENS:
        return None
    t = t.replace("‑", "-").replace("−", "-").replace("–", "-")
    if not NUM_RE.match(t):
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _xcenter(w: dict) -> float:
    return (w["x0"] + w["x1"]) / 2


def _cluster_lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    """按纵坐标聚成行。tol=3pt：同行基线差通常 <2pt，行间距一般 >6pt。"""
    buckets: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for i, (key, group) in enumerate(buckets):
            if abs(key - w["top"]) <= tol:
                group.append(w)
                buckets[i] = (sum(x["top"] for x in group) / len(group), group)
                break
        else:
            buckets.append((w["top"], [w]))
    return [sorted(g, key=lambda w: w["x0"]) for _, g in sorted(buckets, key=lambda b: b[0])]


# ===========================================================================
# 表头识别
# ===========================================================================


def _is_header_line(line: list[dict]) -> bool:
    text = " ".join(w["text"] for w in line).lower()
    return ("tonnes" in text or "tonnage" in text or "矿石量" in text) and (
        "grade" in text or "品位" in text or "%" in text or "g/t" in text
    )


def _header_words(lines: list[list[dict]], idx: int) -> list[dict]:
    """表头词 = 表头行 + 紧随其后的单位行（形如 '(Mt) (g/t) (Moz)'）。"""
    words = list(lines[idx])
    if idx + 1 < len(lines):
        nxt = lines[idx + 1]
        text = " ".join(w["text"] for w in nxt)
        if _HEADER_UNIT_RE.search(text) and not any(k in text.lower() for k in CATEGORIES):
            words += nxt
    return words


def _period_cut(header_words: list[dict]) -> float | None:
    """同表常并排两组年份（本期/上期）。返回第二个 'Tonnes' 表头的横坐标，
    作为「本期列」的右边界；找不到则返回 None（说明只有一组）。"""
    xs = sorted(
        w["x0"]
        for w in header_words
        if "tonnes" in w["text"].lower() or "tonnage" in w["text"].lower()
    )
    return xs[1] if len(xs) >= 2 else None


# ===========================================================================
# 数据驱动定列（核心）
# ===========================================================================


def _data_columns(
    data_lines: list[list[dict]], cut_x: float | None, *, tol: float = 12.0, min_rows: int = 2
) -> list[float]:
    """数据驱动定列：把所有数据行里数字的横坐标聚类，出现得够多的才是真列。"""
    xs: list[float] = []
    for line in data_lines:
        for w in line:
            if _norm_num(w["text"]) is None:
                continue
            if cut_x is not None and w["x0"] >= cut_x:
                continue
            xs.append(_xcenter(w))
    xs.sort()

    clusters: list[list[float]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    # 只有出现在多个数据行里的横坐标才算「列」，滤掉偶发的散数字
    return [sum(c) / len(c) for c in clusters if len(c) >= min_rows]


def _classify_column(center: float, header_words: list[dict], window: float = 32.0) -> dict:
    """给一个数据列判定语义与单位：取横向邻近的表头词拼起来看。"""
    near = [w for w in header_words if abs(_xcenter(w) - center) <= window]
    text = " ".join(w["text"] for w in near).strip()
    low = text.lower()

    kind = None
    for k, hints in _KIND_HINTS:
        if any(h in low for h in hints):
            kind = k
            break

    unit = None
    m = _ANY_PAREN_RE.search(text)
    if m:
        unit = m.group(1).strip()

    return {"center": center, "header_text": text, "kind": kind, "unit": unit}


def _derive_contained(rec: dict) -> dict:
    """表格没给金属量列时，用 吨位 × 品位 推算一个，并**明确标记为推算值**。

    抽取值和推算值必须分开放：抽到的是原文事实，推算是我们的算术。
    混在一起会让下游把推算当原文引用 —— 这正是这类系统最容易骗到人的地方。
    """
    if rec.get("contained") is not None:
        return rec
    t, g = rec.get("tonnes_mt"), rec.get("grade")
    if t is None or g is None:
        return rec

    unit = (rec.get("grade_unit") or "").lower()
    if "g/t" in unit or "ppm" in unit:
        oz = t * 1e6 * g / 31.1034768  # 吨矿 × g/t -> 克 -> 金衡盎司
        rec["contained_derived"] = {
            "value": round(oz, 2),
            "unit": "oz",
            "formula": f"{t} Mt × {g} {rec.get('grade_unit')}",
        }
    elif "%" in unit:
        tonnes_metal = t * g / 100 * 1e6  # Mt × % = Mt 金属 -> 吨
        rec["contained_derived"] = {
            "value": round(tonnes_metal, 1),
            "unit": "t",
            "formula": f"{t} Mt × {g}%",
        }
    return rec


def _clean_project_label(text: str) -> str | None:
    """判断一行是不是「项目名」。排除表头残留、纯占位符和过长的段落文本。"""
    t = re.sub(r"\s+", " ", text).strip(" |·-–—")
    if not t or len(t) > 60:
        return None
    # 全是「无数据」占位符的行（如 "‑ ‑ ‑ ‑ ‑"）不是标签，是某行缺数据的残影
    if not re.sub(r"[‑\-–—\s]", "", t):
        return None
    low = t.lower()
    if any(k in low for k in ("tonnes", "grade", "classification", "mineral resource tonnes")):
        return None
    if low in SUMMARY_LABELS:
        return None
    return t or None


# ===========================================================================
# 单页解析
# ===========================================================================


def _parse_page(lines: list[list[dict]]) -> list[dict]:
    hidx = next((i for i, line in enumerate(lines) if _is_header_line(line)), None)
    if hidx is None:
        return []

    header_words = _header_words(lines, hidx)
    cut_x = _period_cut(header_words)
    # 表头行本身和单位行不作为数据/项目名来源
    units_line = (
        _HEADER_UNIT_RE.search(" ".join(w["text"] for w in lines[hidx + 1]))
        if hidx + 1 < len(lines)
        else None
    )
    body_start = hidx + 2 if units_line else hidx + 1

    data_lines: list[list[dict]] = []
    for line in lines[body_start:]:
        low = " ".join(w["text"] for w in line).lower()
        has_cat = any(k in low for k in CATEGORIES)
        has_sum = any(s in low for s in SUMMARY_LABELS)
        if (has_cat or has_sum) and any(_norm_num(w["text"]) is not None for w in line):
            data_lines.append(line)

    if not data_lines:
        return []

    centers = _data_columns(data_lines, cut_x)
    columns = [_classify_column(c, header_words) for c in centers]
    typed = [c for c in columns if c["kind"]]
    if not typed:
        return []

    rows: list[dict] = []
    current_project: str | None = None

    for line in lines[body_start:]:
        texts = [w["text"] for w in line]
        low = " ".join(texts).lower()
        nums = [w for w in line if _norm_num(w["text"]) is not None]

        if not nums:
            label = _clean_project_label(" ".join(texts))
            if label:
                current_project = label  # 无数字的短行当作项目/分组标题
            elif len(rows) > 2 and not any(k in low for k in CATEGORIES):
                break  # 连续无数字且无分类词 -> 表格结束
            continue

        category = None
        cat_x = None
        for w in line:
            key = w["text"].strip().rstrip(":：").lower()
            if key in CATEGORIES:
                category = CATEGORIES[key]
                cat_x = w["x0"]
                break

        is_summary = any(s in low for s in SUMMARY_LABELS)
        if category is None and not is_summary:
            continue
        if category is None:
            category = "Total"

        project_words = [w["text"] for w in line if cat_x is not None and w["x1"] <= cat_x + 2]
        project = " ".join(project_words).strip()
        if project:
            # 记住它，供后续「Indicated / Inferred」这类省略了项目名的行继承
            current_project = project
        else:
            project = current_project

        record: dict[str, Any] = {
            "project": project,
            "category": category,
            "is_summary_row": is_summary,
            "raw_line": " ".join(texts),
        }
        values: dict[str, dict] = {}

        for w in nums:
            val = _norm_num(w["text"])
            if val is None:
                continue
            wc = _xcenter(w)
            col = min(typed, key=lambda c: abs(c["center"] - wc))
            if abs(col["center"] - wc) > 26:
                continue  # 离所有已知列都太远 -> 丢弃，不硬塞
            values.setdefault(
                col["kind"], {"value": val, "unit": col["unit"], "header": col["header_text"]}
            )

        for kind, got in values.items():
            if kind == "tonnes":
                record["tonnes_mt"] = got["value"]
                record["tonnes_unit"] = got["unit"] or "Mt"
            elif kind == "grade":
                record["grade"] = got["value"]
                record["grade_unit"] = got["unit"]
                record["grade_header"] = got["header"]
            elif kind == "contained":
                record["contained"] = got["value"]
                record["contained_unit"] = got["unit"]
        if len(values) > 1:
            record["all_values"] = {k: v["value"] for k, v in values.items()}

        if any(k in record for k in ("tonnes_mt", "grade", "contained")):
            rows.append(_derive_contained(record))

    return rows


def _table_title(lines: list[list[dict]], header_idx: int) -> str | None:
    for line in reversed(lines[max(0, header_idx - 6): header_idx]):
        text = " ".join(w["text"] for w in line).strip()
        if re.search(r"statement|table\b|resource|表|资源", text, re.I) and len(text) < 130:
            return re.sub(r"\s+", " ", text)
    return None


def _scan_document(path: Path, *, max_pages: int | None) -> dict[str, Any]:
    pages_scanned = 0
    rows: list[dict] = []
    tables: list[dict] = []
    warnings: list[str] = []

    with pdfplumber.open(path) as pdf:
        pages_total = len(pdf.pages)
        limit = pages_total if not max_pages else min(pages_total, max_pages)

        for pno in range(limit):
            page = pdf.pages[pno]
            try:
                raw = page.extract_text() or ""
            except Exception as exc:  # pdfplumber 个别页会解析失败
                warnings.append(f"第 {pno + 1} 页文本层解析失败：{type(exc).__name__}")
                continue
            pages_scanned += 1

            # 只处理同时出现 Indicated/Measured 与 Inferred 的页
            low = raw.lower()
            if "inferred" not in low and "推断" not in low:
                continue
            if not any(k in low for k in ("indicated", "measured", "proved", "控制", "探明")):
                continue

            try:
                words = page.extract_words()
            except Exception as exc:
                warnings.append(f"第 {pno + 1} 页词坐标提取失败：{type(exc).__name__}")
                continue

            lines = _cluster_lines(words)
            hidx = next((i for i, ln in enumerate(lines) if _is_header_line(ln)), None)
            page_rows = _parse_page(lines)
            if not page_rows:
                continue

            title = _table_title(lines, hidx) if hidx is not None else None
            for r in page_rows:
                r["page"] = pno + 1
                r["table_title"] = title
            rows.extend(page_rows)
            tables.append({"page": pno + 1, "rows": len(page_rows), "title": title})

    return {
        "pages_total": pages_total,
        "pages_scanned": pages_scanned,
        "tables": tables,
        "rows": rows,
        "warnings": warnings,
    }


def _resolve_source(pdf_url: str) -> tuple[Path, str, bool]:
    """支持 http(s) 链接与本地路径。返回 (路径, 展示名, 是否临时文件)。"""
    if pdf_url.startswith(("http://", "https://")):
        body, meta = cache.fetch(pdf_url, ttl=86400, retries=2, timeout=120)
        if b"%PDF" not in body[:1024]:
            raise cache.FetchError(pdf_url, "下载到的内容不是 PDF", meta.get("status"))
        # mkstemp 返回的是一个**已打开的**文件描述符，必须立刻关掉：
        # Windows 上持有 fd 会锁住文件，后面 unlink 会抛 WinError 32，
        # 而这个异常会一路冒到 MCP 层变成一句无信息量的 "Error executing tool"。
        fd, name = tempfile.mkstemp(suffix=".pdf", prefix="mpdf_")
        os.close(fd)
        tmp = Path(name)
        tmp.write_bytes(body)
        return tmp, pdf_url, True

    p = Path(pdf_url)
    if not p.exists():
        raise cache.FetchError(pdf_url, "本地文件不存在")
    return p, str(p), False


# ===========================================================================
# 工具
# ===========================================================================


@mcp.tool()
def extract_resources(
    pdf_url: Annotated[str, Field(description="PDF 的 http(s) 链接，或本地文件路径")],
    categories: Annotated[
        list[str] | None,
        Field(description="只要这些分类，如 ['Indicated','Inferred']；不传则全部返回"),
    ] = None,
    max_pages: Annotated[
        int | None, Field(description="最多扫描前 N 页；不传则全篇扫描（大报告较慢）", ge=1)
    ] = None,
) -> dict:
    """从 NI 43-101 / JORC 技术报告 PDF 中抽取储量表（矿石量 / 品位 / 金属量）。

    抽不到会返回 status=no_table_found 与原因，不会返回半成品或推测值。
    """
    try:
        path, display, is_tmp = _resolve_source(pdf_url)
    except cache.FetchError as exc:
        return {
            "source": pdf_url,
            "status": "fetch_failed",
            "reason": exc.reason,
            "status_code": exc.status,
            "resources": [],
        }

    try:
        scan = _scan_document(path, max_pages=max_pages)
    except Exception as exc:
        return {
            "source": display,
            "status": "parse_failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "resources": [],
        }
    finally:
        if is_tmp:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # 临时文件清理失败不该让已经拿到的结果作废

    rows = scan["rows"]
    if categories:
        wanted = {c.strip().lower() for c in categories}
        rows = [r for r in rows if r["category"].lower() in wanted]

    if not rows:
        # 抽不到就明说，不返回半成品，更不编数字
        return {
            "source": display,
            "status": "no_table_found",
            "reason": (
                f"扫描了 {scan['pages_scanned']} 页（共 {scan['pages_total']} 页），"
                "未识别出含 Indicated/Inferred 的储量表。"
                "可能原因：该 PDF 是扫描件（无文本层）、储量表是图片，"
                "或表头结构与已知形态不符。已放弃提取，不做推测。"
            ),
            "pages_scanned": scan["pages_scanned"],
            "pages_total": scan["pages_total"],
            "warnings": scan["warnings"][:5],
            "resources": [],
        }

    return {
        "source": display,
        "status": "ok",
        # 储量数据的权威性是最高的：直接来自上市公司披露原文，不经过任何中间转载或折算
        "source_tier": cache.TIER_OFFICIAL,
        "source_note": "储量数据直接抽取自上市公司披露的报告 PDF 原文，未经转载或折算",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "pages_total": scan["pages_total"],
        "pages_scanned": scan["pages_scanned"],
        "tables": scan["tables"],
        "row_count": len(rows),
        "warnings": scan["warnings"][:5],
        "resources": rows,
    }


@mcp.tool()
def known_reports(
    company: Annotated[
        str | None,
        Field(description="公司/项目名，支持中英与别名（如 'Pilbara'、'皮尔巴拉'）；不传则列出全部"),
    ] = None,
) -> dict:
    """查询已知的储量报告 PDF 索引。

    ⚠️ 这是**人工维护的白名单**，不是全网自动发现。找不到不代表该公司没有报告，
    只代表我们没登记。要拿最新报告，请从新闻正文里找公告链接，或去交易所公告系统查。
    """
    if not company:
        return {"count": len(KNOWN_REPORTS), "reports": KNOWN_REPORTS, "note": "全部已登记的报告"}

    q = company.strip().lower()
    hits = [
        r
        for r in KNOWN_REPORTS
        if q in r["company"].lower()
        or q in r["project"].lower()
        or any(a.strip() and (a.strip() in q or q in a.strip()) for a in r["aliases"].split(","))
    ]
    if not hits:
        return {
            "count": 0,
            "reports": [],
            "queried": company,
            "note": (
                "索引里没有登记这个对象。这不代表不存在报告 —— 请改从新闻正文里找"
                "技术报告/年报链接，或让用户直接提供 PDF 链接。"
            ),
        }
    return {"count": len(hits), "reports": hits, "queried": company}


if __name__ == "__main__":
    mcp.run(transport="stdio")
