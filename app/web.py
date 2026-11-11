"""Flask 采集入口：抓取公示页 → 选择表格/表头行数 → 预览 → 下载 CSV；
以及供水分配页：从转换结果选可供水量列 → 录入灌区 → 字典序最大满足率求解。"""

from __future__ import annotations

import csv
import io
import time
import urllib.parse
from fractions import Fraction
from types import SimpleNamespace
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import (
    Flask,
    Response,
    abort,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .allocation import (
    MAX_DISTRICTS,
    UNIT,
    AllocationError,
    parse_supply_column,
    solve_allocation,
    validate_districts,
)
from .grid_parser import (
    TableParseError,
    lenient_matrix,
    parse_table,
)

app = Flask(__name__)

# 演示用进程内缓存（单 worker 足够）：key -> {"html", "url", "tables"}
_FETCH_CACHE: dict[str, dict] = {}
_FETCH_TTL = 30 * 60  # 30 分钟
_FETCH_MAX = 50

SAMPLE_URL = "/sample/notice.html"
SAMPLE_ERROR_URL = "/sample/notice_error.html"
SAMPLE_SUPPLY_URL = "/sample/notice_supply.html"
USER_AGENT = "HydroTableCollector/1.0 (+reservoir bulletin archiver)"


@app.template_filter("frac")
def frac_filter(value) -> str:
    """最简分数展示：分母为 1 时写作整数，否则 ``分子/分母``。"""
    f = Fraction(value)
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


def _cache_put(key: str, payload: dict) -> None:
    now = time.time()
    expired = [k for k, v in _FETCH_CACHE.items() if now - v["ts"] > _FETCH_TTL]
    for k in expired:
        _FETCH_CACHE.pop(k, None)
    while len(_FETCH_CACHE) >= _FETCH_MAX:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)))
    payload["ts"] = now
    _FETCH_CACHE[key] = payload


def _cache_get(key: str) -> Optional[dict]:
    item = _FETCH_CACHE.get(key)
    if item is None:
        return None
    if time.time() - item["ts"] > _FETCH_TTL:
        _FETCH_CACHE.pop(key, None)
        return None
    return item


def _is_local_sample(url: str) -> bool:
    return url.startswith("/sample/")


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise TableParseError("请输入公示页 URL。")
    if url.startswith(("http://", "https://", "/")):
        return url
    return "https://" + url


def _fetch_html(url: str) -> str:
    if _is_local_sample(url):
        # 本地示例：直接读静态文件，保证离线可演示
        path = url[len("/sample/"):]
        safe = path.replace("/", "_")
        full = app.static_folder + "/samples/" + safe
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise TableParseError(f"本地示例页不存在：{url}（{exc}）")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise TableParseError(f"抓取页面失败：{exc}")

    # requests 按响应头推断编码，推断失败时回退 utf-8
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _parse_tables(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    summaries = []
    for idx, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        cell_count = len(table.find_all(["td", "th"]))
        caption = table.find("caption")
        caption_text = caption.get_text(strip=True) if caption else ""
        # 容错预览：默认按 2 行表头展开
        grid, width = lenient_matrix(table, header_rows=min(2, max(1, len(rows))))
        preview = []
        for line in grid[: 6]:
            preview.append([p.text if p else "" for p in line[: 8]])
        summaries.append(
            {
                "index": idx,
                "n_rows": len(rows),
                "n_cells": cell_count,
                "width": width,
                "caption": caption_text,
                "preview": preview,
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        sample_url=SAMPLE_URL,
        sample_error_url=SAMPLE_ERROR_URL,
        sample_supply_url=SAMPLE_SUPPLY_URL,
    )


@app.post("/fetch")
def fetch():
    raw_url = request.form.get("url", "")
    try:
        url = _normalize_url(raw_url)
        html = _fetch_html(url)
    except TableParseError as exc:
        return render_template("error.html", message=str(exc), stage="抓取"), 400

    soup = BeautifulSoup(html, "html.parser")
    tables_html = soup.find_all("table")
    if not tables_html:
        return render_template(
            "error.html",
            message="该页面没有解析到任何 <table> 表格，请确认 URL 指向公示页。",
            stage="解析",
        ), 400

    summaries = _parse_tables(html)
    cache_key = f"f{int(time.time() * 1000)}-{len(_FETCH_CACHE)}"
    _cache_put(
        cache_key,
        {"html": html, "url": url, "tables": summaries},
    )
    return render_template(
        "select.html",
        cache_key=cache_key,
        url=url,
        tables=summaries,
    )


@app.post("/parse")
def parse():
    cache_key = request.form.get("cache_key", "")
    cached = _cache_get(cache_key)
    if cached is None:
        return render_template(
            "error.html",
            message="会话已过期或缺失，请重新输入 URL 抓取。",
            stage="选择",
        ), 400

    try:
        table_index = int(request.form.get("table_index", ""))
        header_rows = int(request.form.get("header_rows", ""))
    except (TypeError, ValueError):
        return render_template(
            "error.html", message="表格编号或表头行数无效。", stage="选择"
        ), 400

    soup = BeautifulSoup(cached["html"], "html.parser")
    tables = soup.find_all("table")
    if not 0 <= table_index < len(tables):
        abort(400, "表格编号超出范围。")

    try:
        parsed = parse_table(
            tables[table_index], header_rows, source_url=cached["url"]
        )
    except TableParseError as exc:
        return render_template(
            "error.html", message=str(exc), stage="转换", stopped=True
        ), 422

    result_key = f"r{int(time.time() * 1000)}-{len(_FETCH_CACHE)}"
    _cache_put(
        result_key,
        {
            "records": parsed.records,
            "url": cached["url"],
            "header_paths": parsed.header_paths,
            "width": parsed.width,
            "total_rows": parsed.total_rows,
            "header_rows": parsed.header_rows,
        },
    )
    return render_template(
        "preview.html",
        result_key=result_key,
        url=cached["url"],
        parsed=parsed,
        records=parsed.records,
    )


@app.get("/download")
def download():
    result_key = request.args.get("key", "")
    cached = _cache_get(result_key)
    if cached is None:
        return render_template(
            "error.html",
            message="下载会话已过期，请重新抓取并转换。",
            stage="下载",
        ), 400

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "水库",
            "表头路径",
            "原始文本",
            "来源单元格行",
            "来源单元格列",
            "来源单元格坐标",
            "来源URL",
        ]
    )
    for rec in cached["records"]:
        writer.writerow(
            [
                rec["reservoir"],
                rec["header_path"],
                rec["value"],
                rec["source_row"],
                rec["source_col"],
                rec["source_cell"],
                rec["source_url"],
            ]
        )

    # utf-8-sig 便于 Excel 直接打开中文
    payload = buf.getvalue().encode("utf-8-sig")
    fname = "reservoir_observations.csv"
    quoted = urllib.parse.quote(fname)
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename={fname}; filename*=UTF-8''{quoted}"
        },
    )


@app.get("/preview")
def preview():
    """按结果 key 回看转换结果预览（供水分配页之间往返用）。"""
    result_key = request.args.get("key", "")
    cached = _cache_get(result_key)
    if cached is None or "records" not in cached:
        return render_template(
            "error.html",
            message="转换结果会话已过期，请重新抓取并转换。",
            stage="预览",
        ), 400
    parsed = SimpleNamespace(
        width=cached["width"],
        total_rows=cached["total_rows"],
        header_rows=cached["header_rows"],
        header_paths=cached["header_paths"],
    )
    return render_template(
        "preview.html",
        result_key=result_key,
        url=cached["url"],
        parsed=parsed,
        records=cached["records"],
    )


# ---------------------------------------------------------------------------
# 供水分配
# ---------------------------------------------------------------------------

def _render_allocate_input(alloc_key: str, cached: dict, **extra):
    """渲染灌区录入页；extra 可传 error / prefill 等。"""
    prefill = extra.pop("prefill", None)
    if prefill is None:
        prefill = cached.get("last_rows", [])
    initial_rows = prefill or [{"name": "", "demand": "", "sources": []}
                               for _ in range(3)]
    return render_template(
        "allocate.html",
        alloc_key=alloc_key,
        reservoirs=cached["reservoirs"],
        reservoir_names=[r.name for r in cached["reservoirs"]],
        source_url=cached["url"],
        column=cached["column"],
        header_path=cached["header_path"],
        result_key=cached["result_key"],
        max_districts=MAX_DISTRICTS,
        unit=UNIT,
        initial_rows=initial_rows,
        error=extra.pop("error", None),
    )


@app.post("/allocate/start")
def allocate_start():
    """从转换结果按列位置选定可供水量列，进入供水分配页。"""
    result_key = request.form.get("result_key", "")
    cached = _cache_get(result_key)
    if cached is None or "records" not in cached:
        return render_template(
            "error.html",
            message="转换结果会话已过期，请重新抓取并转换后再选择可供水量列。",
            stage="供水分配",
        ), 400

    try:
        column = int(request.form.get("column", ""))
    except (TypeError, ValueError):
        column = -1
    try:
        reservoirs = parse_supply_column(
            cached["records"], column, cached["width"]
        )
    except AllocationError as exc:
        return render_template(
            "error.html", message=str(exc), stage="供水分配"
        ), 422

    header_path = ""
    if 0 < column <= len(cached.get("header_paths", [])):
        header_path = cached["header_paths"][column - 1]

    alloc_key = f"a{int(time.time() * 1000)}-{len(_FETCH_CACHE)}"
    _cache_put(
        alloc_key,
        {
            "reservoirs": reservoirs,
            "result_key": result_key,
            "url": cached["url"],
            "column": column,
            "header_path": header_path,
            "last_rows": [],
        },
    )
    return _render_allocate_input(alloc_key, _cache_get(alloc_key))


@app.get("/allocate")
def allocate_page():
    """按会话 key 重回灌区录入页（保留上次提交内容）。"""
    alloc_key = request.args.get("key", "")
    cached = _cache_get(alloc_key)
    if cached is None or "reservoirs" not in cached:
        return render_template(
            "error.html",
            message="供水分配会话已过期，请从转换结果重新选择可供水量列。",
            stage="供水分配",
        ), 400
    return _render_allocate_input(alloc_key, cached)


@app.post("/allocate/solve")
def allocate_solve():
    """校验灌区录入并求字典序最大满足率的最优分配。"""
    alloc_key = request.form.get("alloc_key", "")
    cached = _cache_get(alloc_key)
    if cached is None or "reservoirs" not in cached:
        return render_template(
            "error.html",
            message="供水分配会话已过期，请从转换结果重新选择可供水量列。",
            stage="供水分配",
        ), 400

    reservoirs = cached["reservoirs"]
    rows = []
    for k in request.form.getlist("didx"):
        rows.append(
            (
                request.form.get(f"name_{k}", ""),
                request.form.get(f"demand_{k}", ""),
                request.form.getlist(f"src_{k}"),
            )
        )
    prefill = [
        {"name": n, "demand": d, "sources": list(s)} for (n, d, s) in rows
    ]
    try:
        districts = validate_districts(rows, [r.name for r in reservoirs])
    except AllocationError as exc:
        return _render_allocate_input(
            alloc_key, cached, error=str(exc), prefill=prefill
        ), 400

    # 保留本次录入，便于结果页「调整灌区重新求解」
    cached["last_rows"] = [
        {"name": d.name, "demand": str(d.demand), "sources": list(d.sources)}
        for d in districts
    ]
    result = solve_allocation(reservoirs, districts)

    district_rows = []
    for i, d in enumerate(districts):
        district_rows.append(
            {
                "name": d.name,
                "demand": d.demand,
                "sources_text": "、".join(d.sources) if d.sources else "（无水源）",
                "no_source": not d.sources,
                "rate": result.rates[i],
                "received": result.received[i],
                "gap": result.gaps[i],
            }
        )
    matrix_rows = []
    for r, reservoir in enumerate(reservoirs):
        used = result.reservoir_used[r]
        matrix_rows.append(
            {
                "name": reservoir.name,
                "supply": reservoir.supply,
                "cells": result.flows[r],
                "used": used,
                "left": Fraction(reservoir.supply) - used,
            }
        )
    sorted_items = [
        {"name": districts[i].name, "rate": rate}
        for i, rate in result.sorted_rates
    ]
    return render_template(
        "allocate_result.html",
        alloc_key=alloc_key,
        result_key=cached["result_key"],
        source_url=cached["url"],
        column=cached["column"],
        header_path=cached["header_path"],
        unit=UNIT,
        districts=districts,
        district_rows=district_rows,
        matrix_rows=matrix_rows,
        sorted_items=sorted_items,
        total_supply=sum(r.supply for r in reservoirs),
        total_demand=sum(d.demand for d in districts),
        total_received=sum(result.received, Fraction(0)),
        total_gap=sum(result.gaps, Fraction(0)),
    )


@app.get("/sample/<path:filename>")
def sample(filename: str):
    return send_from_directory(
        app.static_folder + "/samples", filename, mimetype="text/html"
    )


@app.errorhandler(404)
def not_found(_err):
    return render_template("error.html", message="页面不存在。", stage="路由"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
