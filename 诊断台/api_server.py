#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断台 · 零依赖后端 HTTP 服务（Phase 3 前端承载）

技术选型：标准库 http.server，零三方依赖（与 scheduler.py 同风格）。
职责：
  - 托管 web/ 下的前端静态文件
  - 提供 JSON API，背靠现有 SQLite + tools.py / orchestrator.py / ingest.py

API：
  GET  /api/customers          客户列表（支持 industry/status/source/q 筛选；附本周 KPI + 环比；light=1 跳过 KPI）
  GET  /api/daily              全部客户×全部日期的日度聚合（含派生 ctr/br/lcvr/cpc/cpl），供前端时间筛选窗口计算
  GET  /api/base?start=&end=&prev_start=&prev_end=   基建情况（在投/新投 笔记与计划，窗口感知）
  GET  /api/weeks?customer_id= 该客户可用的自然周（周一）列表
  GET  /api/report?customer_id=&week=&metric_threshold=&spend_threshold=&dry_run=
                               生成某客户某周诊断报告（默认 dry_run，离线确定性，秒级）
  POST /api/report             JSON body: {customer_id, cur_start, cur_end, cmp_start, cmp_end, dry_run}
                               真实 LLM 生成八章节报告 + 落盘 web/reports/<客户>_<日期>.html，返回 {report, share_url}
  GET  /api/compare?customer_id=&weeks=w1,w2,w3   多周指标对比
  GET  /api/cases?industry=&sector=&signature=    案例检索（现有 SQL 匹配 RAG）
  POST /api/ingest             手动录入客户（接 ingest.ingest_customer）
  POST /api/review             JSON body: {report_id, action:approve|reject, reason, reviewer}
                               审核闭环：通过→report_review落confirm+自动沉淀diag_case（RAG案例库）；
                               驳回→report_review落reject+缺陷记入diag_badcase（open，待修复）

用法：
  python3 api_server.py                 # 默认 http://127.0.0.1:8000
  python3 api_server.py --port 9000
  python3 api_server.py --db data/ad_review.db
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HERE, "agent")
DATA_DIR = os.path.join(HERE, "data")
WEB_DIR = os.path.join(HERE, "web")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, DATA_DIR)

import tools
import orchestrator
import ingest as ingest_mod
import review_actions

DEFAULT_DB = os.path.join(HERE, "data", "ad_review.db")


# ---------------------------------------------------------------- 工具
def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _d(s):
    return date.fromisoformat(s)


def week_start_of(d):
    """返回 d 所在自然周的周一"""
    return d - timedelta(days=d.weekday())


def available_weeks(conn, customer_id):
    """该客户有数据的自然周（周一）列表，倒序"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_metric WHERE customer_id=?", (customer_id,)).fetchall()
    if not rows:
        return []
    weeks = set()
    for r in rows:
        weeks.add(week_start_of(_d(r["date"])))
    return sorted(weeks, reverse=True)


def customer_kpi(conn, customer_id):
    """本周 vs 上周 的核心 KPI（用于列表/总览卡片）"""
    cs, ce, ps, pe = orchestrator.derive_periods(conn, customer_id)
    comp = tools.get_period_comparison(conn, customer_id, cs, ce, ps, pe)
    mc, mp, chg = comp["metrics_cur"], comp["metrics_prev"], comp["metrics_change"]
    cur_spend = (comp.get("cur") or {}).get("spend")
    prev_spend = (comp.get("prev") or {}).get("spend")
    spend_change = tools.pct_change(cur_spend, prev_spend) if (cur_spend is not None and prev_spend is not None) else None
    target_cost_metric = "open_cost" if (tools.get_customer_profile(conn, customer_id).get("optimize_target") == "open") else "lead_cost"
    return {
        "cur_start": cs, "cur_end": ce, "cmp_start": ps, "cmp_end": pe,
        "spend": cur_spend, "spend_change": spend_change,
        "lead_cnt": mc.get("lead_cnt"), "open_msg": mc.get("open_msg"),
        "lead_cost": mc.get(target_cost_metric), "lead_cost_change": chg.get(target_cost_metric),
        "open_cost": mc.get("open_cost"), "open_cost_change": chg.get("open_cost"),
        "ctr": mc.get("CTR"), "ctr_change": chg.get("CTR"),
    }


# ---------------------------------------------------------------- 路由实现
def api_customers(db_path, params):
    conn = get_conn(db_path)
    industry = (params.get("industry", [""])[0] or "").strip()
    status = (params.get("status", [""])[0] or "").strip()
    source = (params.get("source", [""])[0] or "").strip()
    q = (params.get("q", [""])[0] or "").strip()
    sql = """SELECT c.id, c.name, c.optimize_target, c.target_cost, c.status, c.source,
                    i.name AS industry, s.name AS sector
             FROM customer c
             JOIN sector s ON s.id=c.sector_id
             JOIN industry i ON i.id=s.industry_id
             WHERE 1=1"""
    args = []
    if industry:
        sql += " AND i.name LIKE ?"; args.append(f"%{industry}%")
    if status:
        sql += " AND c.status = ?"; args.append(status)
    if source:
        sql += " AND c.source = ?"; args.append(source)
    if q:
        sql += " AND c.name LIKE ?"; args.append(f"%{q}%")
    sql += " ORDER BY c.id"
    light = (params.get("light", [""])[0] or "").strip() == "1"
    out = []
    for r in conn.execute(sql, args).fetchall():
        cats = [x["name"] for x in conn.execute(
            """SELECT k.name FROM category k JOIN customer_category cc ON cc.category_id=k.id
               WHERE cc.customer_id=?""", (r["id"],))]
        item = {k: r[k] for k in ("id", "name", "optimize_target", "target_cost",
                                  "status", "source", "industry", "sector")}
        item["categories"] = cats
        if not light:
            try:
                item["kpi"] = customer_kpi(conn, r["id"])
            except Exception:
                item["kpi"] = None
        out.append(item)
    conn.close()
    return out


def api_weeks(db_path, params):
    cid = int(params.get("customer_id", ["0"])[0])
    conn = get_conn(db_path)
    weeks = available_weeks(conn, cid)
    conn.close()
    return [w.isoformat() for w in weeks]


def api_report(db_path, params):
    cid = int(params.get("customer_id", ["0"])[0])
    week = (params.get("week", [""])[0] or "").strip()
    mt = params.get("metric_threshold", [""])[0]
    st = params.get("spend_threshold", [""])[0]
    dry_run = (params.get("dry_run", ["true"])[0]).lower() != "false"
    mt = float(mt) if mt else None
    st = float(st) if st else None
    conn = get_conn(db_path)
    if week:
        ws = _d(week)
        cs, ce = ws, ws + timedelta(days=6)
        ps, pe = ws - timedelta(days=7), ws - timedelta(days=1)
    else:
        cs, ce, ps, pe = orchestrator.derive_periods(conn, cid)
    o = orchestrator.ReviewOrchestrator(db_path)
    try:
        report = o.run(customer_id=cid, dry_run=dry_run,
                       metric_threshold=mt, spend_threshold=st,
                       cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe)
    finally:
        o.close()
    conn.close()
    if "error" in report:
        return {"error": report["error"]}
    report["params"] = {"week": cs, "metric_threshold": mt, "spend_threshold": st, "dry_run": dry_run}
    return report


def api_compare(db_path, params):
    cid = int(params.get("customer_id", ["0"])[0])
    weeks = [w for w in (params.get("weeks", [""])[0] or "").split(",") if w]
    conn = get_conn(db_path)
    prof = tools.get_customer_profile(conn, cid)
    target_cost_metric = "open_cost" if prof.get("optimize_target") == "open" else "lead_cost"
    out = []
    for w in weeks:
        try:
            ws = _d(w)
        except Exception:
            continue
        cs, ce = ws, ws + timedelta(days=6)
        ps, pe = ws - timedelta(days=7), ws - timedelta(days=1)
        comp = tools.get_period_comparison(conn, cid, cs, ce, ps, pe)
        mc, chg = comp["metrics_cur"], comp["metrics_change"]
        out.append({
            "week": w, "week_label": f"{cs}~{ce}",
            "spend": (comp.get("cur") or {}).get("spend"),
            "lead_cnt": mc.get("lead_cnt"), "open_msg": mc.get("open_msg"),
            "lead_cost": mc.get(target_cost_metric), "open_cost": mc.get("open_cost"),
            "ctr": mc.get("CTR"), "cpm": mc.get("CPM"), "cpc": mc.get("CPC"),
            "spend_change": comp.get("spend_change"),
            "lead_cost_change": chg.get(target_cost_metric),
            "open_cost_change": chg.get("open_cost"),
            "ctr_change": chg.get("CTR"),
        })
    conn.close()
    return {"customer": prof.get("name"), "rows": out}


def api_cases(db_path, params):
    industry = (params.get("industry", [""])[0] or "").strip() or None
    sector = (params.get("sector", [""])[0] or "").strip() or None
    signature = (params.get("signature", [""])[0] or "").strip()
    sig_terms = [t.strip() for t in signature.split(",") if t.strip()] or None
    conn = get_conn(db_path)
    res = tools.search_cases(conn, industry=industry, sector=sector, signature_terms=sig_terms)
    conn.close()
    return res


def api_ingest(db_path, body):
    try:
        payload = json.loads(body)
    except Exception as e:
        return {"error": f"JSON 解析失败: {e}"}
    conn = get_conn(db_path)
    try:
        res = ingest_mod.ingest_customer(conn, payload)
    except Exception as e:
        conn.close()
        return {"error": f"录入失败: {type(e).__name__}: {e}"}
    conn.close()
    return res


def api_daily(db_path, params):
    """全部客户×全部日期的日度聚合（客户级，含派生指标），供前端任意时间窗口计算"""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT c.name AS name, dm.date AS date,
               SUM(dm.spend) AS spend, SUM(dm.impressions) AS imp,
               SUM(dm.note_clicks) AS nc, SUM(dm.button_clicks) AS bc,
               SUM(dm.open_msg) AS open, SUM(dm.lead_cnt) AS lead
        FROM daily_metric dm JOIN customer c ON c.id=dm.customer_id
        GROUP BY dm.customer_id, dm.date
        ORDER BY dm.date
    """).fetchall()
    conn.close()
    out, maxd = {}, ""
    for r in rows:
        d = {k: r[k] for k in ("date", "spend", "imp", "nc", "bc", "open", "lead")}
        if d["date"] > maxd:
            maxd = d["date"]
        d["ctr"] = round(d["nc"] / d["imp"] * 100, 2) if d["imp"] else 0
        d["br"] = round(d["bc"] / d["nc"] * 100, 2) if d["nc"] else 0
        d["lcvr"] = round(d["lead"] / d["open"] * 100, 2) if d["open"] else 0
        d["cpc"] = round(d["spend"] / d["nc"], 2) if d["nc"] else 0
        d["cpl"] = round(d["spend"] / d["lead"], 2) if d["lead"] else 0
        out.setdefault(r["name"], []).append(d)
    return {"maxd": maxd, "customers": out}


def api_base(db_path, params):
    """基建情况：在投笔记/计划（按窗口末日快照）+ 新投笔记/计划（窗口内创建，含上一窗口环比基数）"""
    start = (params.get("start", [""])[0] or "").strip()
    end = (params.get("end", [""])[0] or "").strip()
    prev_start = (params.get("prev_start", [""])[0] or "").strip()
    prev_end = (params.get("prev_end", [""])[0] or "").strip()
    conn = get_conn(db_path)

    def cnt(sql, args):
        return conn.execute(sql, args).fetchone()[0]

    def in_flight(table, asof):
        return cnt(f"SELECT COUNT(*) FROM {table} WHERE created_date<=? "
                   f"AND (stopped_date IS NULL OR stopped_date>?)", (asof, asof))

    out = {"in_notes": 0, "in_plans": 0, "new_notes": 0, "new_plans": 0,
           "prev_new_notes": 0, "prev_new_plans": 0,
           "prev_in_notes": 0, "prev_in_plans": 0}
    if end:
        out["in_notes"] = in_flight("note", end)
        out["in_plans"] = in_flight("plan", end)
        out["new_notes"] = cnt("SELECT COUNT(*) FROM note WHERE created_date BETWEEN ? AND ?", (start, end))
        out["new_plans"] = cnt("SELECT COUNT(*) FROM plan WHERE created_date BETWEEN ? AND ?", (start, end))
    if prev_end:
        out["prev_in_notes"] = in_flight("note", prev_end)
        out["prev_in_plans"] = in_flight("plan", prev_end)
        out["prev_new_notes"] = cnt("SELECT COUNT(*) FROM note WHERE created_date BETWEEN ? AND ?", (prev_start, prev_end))
        out["prev_new_plans"] = cnt("SELECT COUNT(*) FROM plan WHERE created_date BETWEEN ? AND ?", (prev_start, prev_end))
    conn.close()
    return out


# ---------------------------------------------------------------- 报告独立 HTML 渲染
def _num(v):
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"
    return str(v)


def _pct(v):
    if isinstance(v, (int, float)):
        return f"{v*100:+.1f}%" if abs(v) <= 10 else f"{v:+.1f}%"
    return str(v)


METRIC_CN = {"CPM": "CPM", "CTR": "CTR", "CPC": "CPC", "button_rate": "按钮率",
             "open_rate": "开口率", "lead_rate": "留资率", "lead_cvr": "留资转化率",
             "open_cost": "开口成本", "lead_cost": "留资成本"}
LAYER_CN = {"placement": "版位", "plan": "计划", "note": "笔记", "funnel": "漏斗"}
# 比率类指标（派生值为小数，渲染时需 ×100 并补 %）
RATE_METRICS = {"CTR", "button_rate", "open_rate", "lead_rate", "lead_cvr"}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trend_svg(t, mname):
    """第 3 章趋势折线图（与站内当日监控分面小图同款视觉：折线+均值虚线+端点标注+斜放横轴）"""
    if not isinstance(t, dict):
        return ""
    daily = [d for d in (t.get("daily") or []) if d.get("value") is not None]
    if len(daily) < 2:
        return ""
    vals = [d["value"] for d in daily]
    W, H, PL, PR, PT, PB = 420, 170, 44, 14, 12, 30
    w, h = W - PL - PR, H - PT - PB
    mx, mn = max(vals), min(vals)
    rng = (mx - mn) or 1

    def X(i):
        return PL + i * w / (len(vals) - 1)

    def Y(v):
        return PT + h - (v - mn) / rng * h

    path = "".join(f"{'M' if i == 0 else 'L'}{X(i):.1f} {Y(v):.1f}" for i, v in enumerate(vals))
    avg = sum(vals) / len(vals)

    def fv(v):
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.2f}"

    step = max(1, -(-len(vals) // 5))
    ticks = list(range(0, len(vals), step))
    if ticks[-1] != len(vals) - 1:
        ticks.append(len(vals) - 1)
    xt = "".join(
        f"<text class='ax' x='{X(i):.1f}' y='{PT+h+12:.1f}' text-anchor='end' "
        f"transform='rotate(-35,{X(i):.1f},{PT+h+12:.1f})'>{_esc(daily[i]['date'][5:])}</text>"
        for i in ticks)
    dots = "".join(
        f"<circle cx='{X(i):.1f}' cy='{Y(v):.1f}' r='2.2' fill='#1E6FD9' opacity='{1 if i == len(vals)-1 else 0.45}'/>"
        for i, v in enumerate(vals))
    return (f"<div class='trendbox'><div class='trendhead'><span>近14天{_esc(mname)}趋势</span>"
            f"<span class='muted'>均值 ¥{fv(avg)}</span></div>"
            f"<svg viewBox='0 0 {W} {H}'>"
            f"<line x1='{PL}' y1='{PT+h:.1f}' x2='{PL+w:.1f}' y2='{PT+h:.1f}' stroke='#E9EDF5'/>"
            f"<line x1='{PL}' y1='{Y(avg):.1f}' x2='{PL+w:.1f}' y2='{Y(avg):.1f}' stroke='#94A3B8' stroke-dasharray='4 4' stroke-width='1'/>"
            f"<path d='{path}' fill='none' stroke='#1E6FD9' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/>"
            f"{dots}"
            f"<circle cx='{X(len(vals)-1):.1f}' cy='{Y(vals[-1]):.1f}' r='3.6' fill='#FFF' stroke='#1E6FD9' stroke-width='2.5'/>"
            f"<text class='ax' x='{PL-4}' y='{PT+7}' text-anchor='end'>¥{fv(mx)}</text>"
            f"<text class='ax' x='{PL-4}' y='{PT+h+3:.1f}' text-anchor='end'>¥{fv(mn)}</text>"
            f"{xt}</svg></div>")


def _clean_anomaly(sig):
    """anomaly_signature 可能是 dict 或字符串。统一提取可读的 location+reason."""
    if sig is None:
        return ""
    if isinstance(sig, dict):
        loc = sig.get("location", "")
        reason = sig.get("reason", "")
        return f"{loc}：{reason}" if loc or reason else json.dumps(sig, ensure_ascii=False)
    s = str(sig).strip()
    if s.startswith("{") and ("'" in s or '"' in s):
        try:
            d = eval(s, {"__builtins__": {}}, {})
            if isinstance(d, dict):
                loc = d.get("location", "")
                reason = d.get("reason", "")
                if loc or reason:
                    return f"{loc}：{reason}"
        except Exception:
            pass
        # 退路：截断/畸形字典串，用正则抽 location / reason
        import re
        ml = re.search(r"['\"]location['\"]\s*:\s*['\"]([^'\"]*)", s)
        mr = re.search(r"['\"]reason['\"]\s*:\s*['\"]([^'\"]*)", s)
        if ml or mr:
            loc = ml.group(1) if ml else ""
            reason = mr.group(1) if mr else ""
            if loc or reason:
                return f"{loc}：{reason}"
    return s


def render_report_html(report):
    """把八章节报告 JSON 渲染为自包含 HTML（分享链接落盘文件）"""
    ch = report.get("chapters", {})
    cover = ch.get("1_封面", {})
    concl = ch.get("2_核心结论", {})
    metrics = ch.get("3_指标与趋势", {})
    layers = ch.get("4_分层诊断", [])
    anomalies = ch.get("5_异常与原因", {})
    cases = ch.get("6_案例参考", {})
    suggests = ch.get("7_优化建议", [])
    actions = ch.get("8_行动计划", [])
    mc, mp = metrics.get("metrics_cur", {}), metrics.get("metrics_prev", {})

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    rows = ""
    for k in mc:
        cur, prev = mc.get(k), mp.get(k)
        if isinstance(cur, (int, float)) and isinstance(prev, (int, float)) and prev:
            chg = f"{(cur-prev)/prev*100:+.1f}%"
        else:
            chg = "—"
        cfmt = lambda v: f"{v*100:.2f}%" if isinstance(v, (int, float)) else "—"
        ccur = cfmt(cur) if k in RATE_METRICS else _num(cur)
        cprev = cfmt(prev) if k in RATE_METRICS else _num(prev)
        rows += f"<tr><td>{esc(METRIC_CN.get(k, k))}</td><td>{ccur}</td><td>{cprev}</td><td>{chg}</td></tr>"

    layer_html = "".join(
        f"<div class='item'><b>{esc(LAYER_CN.get(x.get('layer'), x.get('layer')))}</b> · <span class='st'>{esc(x.get('status'))}</span>"
        f"<p>{esc(x.get('judgement',''))}</p></div>" for x in layers)

    td = anomalies.get("top3_detail", [])
    if isinstance(td, str):
        anom_html = f"<p>{esc(td)}</p>"
    elif isinstance(td, list):
        anom_html = "".join(
            f"<div class='item'><b>{x.get('rank')} {esc(tools.norm_report_text(x.get('location','')))}</b>"
            f"<p>{esc(tools.norm_report_text(x.get('reason','')))}</p><ul>"
            + "".join(f"<li>{esc(tools.norm_report_text(e))}</li>" for e in x.get("evidence", []))
            + "</ul></div>" for x in td)
    else:
        anom_html = ""

    case_refs = cases.get("refs") or cases.get("cases") or []
    _PH = ("（待补", "待 LLM", "审核时补充", "{}", "[]")

    def _ph(s):
        s = "" if s is None else str(s).strip()
        return (s == "" or s in ("{}", "[]") or any(p in s for p in _PH))

    if case_refs:
        parts = []
        for c in case_refs:
            sig = _clean_anomaly(c.get("anomaly_signature", ""))
            act = "" if _ph(c.get("action_taken")) else tools.norm_report_text(c.get("action_taken", ""))
            sim = "" if _ph(c.get("similarity_points")) else tools.norm_report_text(c.get("similarity_points", ""))
            if len(sim) > 220:
                sim = sim[:220] + "…"
            line = f"<div class='item'><b>案例 #{esc(c.get('case_id'))}</b>"
            if c.get("sector"):
                line += f" <span class='tag'>{esc(c.get('sector'))}</span>"
            if sig:
                line += f"<p>异常：{esc(sig)}</p>"
            if act:
                line += f"<p>打法：{esc(act)}</p>"
            if sim:
                line += f"<p class='muted'>相似点：{esc(sim)}</p>"
            line += "</div>"
            parts.append(line)
        case_html = "".join(parts)
    else:
        case_html = f"<p class='muted'>{esc(cases.get('note', '暂无可引用案例'))}</p>"

    sug_html = "".join(
        f"<div class='item'><span class='tag {esc(x.get('priority',''))}'>{esc(x.get('priority'))}</span>"
        f"<p>{esc(tools.norm_report_text(x.get('text','')))}</p><p class='muted'>依据：{esc(tools.norm_report_text(x.get('basis','')))}</p></div>"
        for x in suggests)

    act_rows = "".join(
        f"<tr><td>{esc(tools.norm_report_text(x.get('action','')))}</td><td>{esc(x.get('date',''))}</td>"
        f"<td>{esc(tools.norm_report_text(x.get('expect_metric','')))}</td></tr>" for x in actions)

    top3_html = "".join(
        f"<li>{esc(tools.norm_report_text(x.get('location','')))}：{_pct(x.get('change', 0))}（权重 {x.get('weight')}）</li>"
        for x in concl.get("top3", []))

    status = report.get("overall_status", concl.get("overall_status", ""))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(cover.get('customer',''))} · 复盘报告</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#F8F9FC;color:#1F2937;font-size:13px;line-height:1.7;margin:0;padding:24px;}}
.wrap{{max-width:860px;margin:0 auto;}}
.cover{{background:linear-gradient(135deg,#1E6FD9,#3B9DFF);color:#fff;border-radius:12px;padding:26px;margin-bottom:16px;}}
.cover h1{{font-size:22px;margin:0 0 6px;}}
.cover .meta{{opacity:.92;font-size:12px;}}
.card{{background:#fff;border:1px solid #E9EDF5;border-radius:12px;padding:18px;margin-bottom:14px;}}
.card h3{{font-size:14px;color:#1E6FD9;margin:0 0 10px;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
th{{text-align:left;color:#94A3B8;font-size:11px;padding:7px 8px;border-bottom:1px solid #E9EDF5;}}
td{{padding:7px 8px;border-bottom:1px solid #E9EDF5;}}
.item{{margin-bottom:12px;}}
.item p{{margin:4px 0;}}
.muted{{color:#94A3B8;font-size:11px;}}
.tag{{display:inline-block;padding:1px 8px;border-radius:20px;font-size:10px;font-weight:800;color:#fff;margin-right:6px;}}
.tag.P0{{background:#F97066;}} .tag.P1{{background:#3B9DFF;}}
.st{{color:#3B9DFF;font-weight:700;}}
.trendbox{{margin:10px 0 4px;}}
.trendhead{{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;}}
.trendbox svg{{width:100%;height:auto;}}
.trend-pair{{display:flex;gap:14px;}}
.trend-pair > div{{flex:1;min-width:0;}}
@media(max-width:640px){{.trend-pair{{flex-direction:column;}}}}
.ax{{font-size:10px;fill:#94A3B8;}}
.status{{display:inline-block;padding:2px 10px;border-radius:20px;font-weight:800;font-size:12px;}}
.status.需行动{{background:#FEE4E2;color:#B42318;}} .status.观察{{background:#FEF0C7;color:#B54708;}} .status.正常{{background:#D1FADF;color:#027A48;}}
</style></head><body><div class="wrap">
<div class="cover"><h1>{esc(cover.get('customer',''))} · 周度复盘报告</h1>
<div class="meta">{esc(cover.get('industry',''))} / {esc(cover.get('sector',''))} / {esc('、'.join(cover.get('categories',[])))}</div>
<div class="meta">周期 {esc(cover.get('period',''))} · 生成于 {esc(cover.get('generated_at',''))}</div></div>
<div class="card"><h3>① 核心结论</h3>
<p><span class="status {esc(status)}">{esc(status)}</span> 数据状态：{esc(concl.get('data_status',''))}</p>
<p>{esc(tools.norm_report_text(concl.get('summary','')))}</p>
<ul>{top3_html}</ul></div>
<div class="card"><h3>② 指标与趋势</h3>
<table><thead><tr><th>指标</th><th>本期</th><th>上期</th><th>环比</th></tr></thead><tbody>{rows}</tbody></table>
<div class="trend-pair">{_trend_svg(metrics.get('trend_spend'), '消耗')}
{_trend_svg(metrics.get('trend_14d'), METRIC_CN.get(metrics.get('trend_metric')) or metrics.get('trend_metric') or '目标成本')}</div></div>
<div class="card"><h3>③ 分层诊断</h3>{layer_html or "<p class='muted'>无</p>"}</div>
<div class="card"><h3>④ 异常与原因</h3>{anom_html or "<p class='muted'>无明显异常</p>"}</div>
<div class="card"><h3>⑤ 案例参考</h3>{case_html}</div>
<div class="card"><h3>⑥ 优化建议</h3>{sug_html or "<p class='muted'>正常周无待办建议</p>"}</div>
<div class="card"><h3>⑦ 行动计划</h3>
<table><thead><tr><th>行动</th><th>日期</th><th>预期指标</th></tr></thead><tbody>{act_rows}</tbody></table></div>
<div class="card"><h3>⑧ 数据质量说明</h3><p class="muted">本期数据状态：{esc(concl.get('data_status',''))}；
缺失字段（本期）：{esc(concl.get('data_check',{}).get('cur_missing') or '无')}；
缺失字段（上期）：{esc(concl.get('data_check',{}).get('prev_missing') or '无')}。
LLM 调用 {report.get('llm_calls','-')} 次，成本 ¥{report.get('llm_cost_yuan','-')}。</p></div>
</div></body></html>"""


def attrib_reason(conn, customer_id, cs, ce, ps, pe):
    """确定性归因一句话：复用 tools 的环比对比 + 基建双门槛，无 LLM、无任务落库"""
    comp = tools.get_period_comparison(conn, customer_id, cs, ce, ps, pe)
    if not comp["cur"]["spend"] and not comp["prev"]["spend"]:
        return "该窗口无消耗数据"
    chg, mc, mp = comp["metrics_change"], comp["metrics_cur"], comp["metrics_prev"]
    sc = comp["spend_change"]
    parts = []
    infra = tools.get_infrastructure(conn, customer_id, cs, ce, ps, pe)
    ng, pg = infra["note_gate"], infra["plan_gate"]
    if ng["hit"]:
        parts.append(f"在投笔记 {ng['prev']}→{ng['cur']}" + ("（基建掉量）" if ng["delta"] < 0 else "（基建扩量）"))
    elif pg["hit"]:
        parts.append(f"在投计划 {pg['prev']}→{pg['cur']}" + ("（基建掉量）" if pg["delta"] < 0 else "（基建扩量）"))
    ctr = chg.get("CTR")
    if ctr is not None and abs(ctr) >= 0.10 and mc.get("CTR") is not None and mp.get("CTR") is not None:
        tag = "素材吸引力下降" if ctr < 0 else "素材吸引力提升"
        parts.append(f"CTR {mp['CTR']*100:.2f}%→{mc['CTR']*100:.2f}%，{tag}")
    lr = chg.get("lead_rate")
    if lr is not None and lr <= -0.10:
        parts.append("开口→留资转化下滑")
    lc = chg.get("lead_cost")
    if lc is not None and abs(lc) >= 0.15 and mc.get("lead_cost") is not None and mp.get("lead_cost") is not None:
        parts.append(f"留资成本 ¥{mp['lead_cost']:.0f}→¥{mc['lead_cost']:.0f}")
    head = f"消耗环比 {sc:+.0%}" if sc is not None else "消耗环比 —"
    if not parts:
        return head + "，各环节指标未见显著异常"
    return head + "：" + "；".join(parts[:3])


def api_attrib(db_path, body):
    """POST /api/attrib：一批客户的确定性归因文案，供首页掉量/增量 TOP 使用
    body: {ids:[customer_id...], cur_start, cur_end, cmp_start, cmp_end}"""
    try:
        payload = json.loads(body)
    except Exception as e:
        return {"error": f"JSON 解析失败: {e}"}
    ids = payload.get("ids") or []
    cs, ce = payload.get("cur_start"), payload.get("cur_end")
    ps, pe = payload.get("cmp_start"), payload.get("cmp_end")
    if not (ids and cs and ce and ps and pe):
        return {"error": "需要 ids 与 cur_start/cur_end/cmp_start/cmp_end"}
    conn = get_conn(db_path)
    out = {}
    try:
        for cid in ids:
            try:
                out[str(int(cid))] = attrib_reason(conn, int(cid), cs, ce, ps, pe)
            except Exception as e:
                out[str(cid)] = f"归因失败: {type(e).__name__}"
    finally:
        conn.close()
    return {"reasons": out}


def api_report_generate(db_path, body):
    """POST /api/report：真实 LLM 生成报告 + 落盘独立 HTML，返回 {report, share_url}"""
    try:
        payload = json.loads(body)
    except Exception as e:
        return {"error": f"JSON 解析失败: {e}"}
    try:
        cid = int(payload.get("customer_id", 0))
    except Exception:
        return {"error": "customer_id 无效"}
    dry_run = bool(payload.get("dry_run", False))
    cs, ce = payload.get("cur_start"), payload.get("cur_end")
    ps, pe = payload.get("cmp_start"), payload.get("cmp_end")
    if not (cs and ce and ps and pe):
        conn = get_conn(db_path)
        cs, ce, ps, pe = orchestrator.derive_periods(conn, cid)
        conn.close()
    o = orchestrator.ReviewOrchestrator(db_path)
    try:
        report = o.run(customer_id=cid, dry_run=dry_run,
                       cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe)
    finally:
        o.close()
    if "error" in report:
        return {"error": report["error"]}
    conn = get_conn(db_path)
    row = conn.execute("SELECT name FROM customer WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        return {"error": f"客户不存在: {cid}"}
    fname = f"{row['name']}_{ce}.html"
    fpath = os.path.join(WEB_DIR, "reports", fname)
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(render_report_html(report))
    conn = get_conn(db_path)
    rid = conn.execute("SELECT id FROM report WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (report.get("task_id"),)).fetchone()
    ncase = conn.execute("SELECT COUNT(*) n FROM diag_case").fetchone()["n"]
    cust_case = conn.execute("SELECT COUNT(*) n FROM diag_case WHERE customer_id=?",
                             (cid,)).fetchone()["n"]
    conn.close()
    report["report_id"] = rid["id"] if rid else None
    return {"report": report, "share_url": "/reports/" + fname,
            "report_id": report["report_id"], "case_count": ncase,
            "customer_has_case": cust_case > 0, "customer_case_count": cust_case}


def api_review(db_path, body):
    """POST /api/review：报告审核闭环（approve→入案例库；reject→记 badcase）"""
    try:
        payload = json.loads(body)
    except Exception as e:
        return {"error": f"JSON 解析失败: {e}"}
    try:
        report_id = int(payload.get("report_id", 0))
    except Exception:
        return {"error": "report_id 无效"}
    action = payload.get("action")
    reason = (payload.get("reason") or "").strip()
    reviewer = payload.get("reviewer") or "web"
    conn = get_conn(db_path)
    try:
        if action == "approve":
            res = review_actions.approve_report(conn, report_id, reviewer=reviewer)
            if "error" in res:
                return res
            promo = review_actions.promote_to_case(conn, report_id)
            ncase = conn.execute("SELECT COUNT(*) n FROM diag_case").fetchone()["n"]
            return {"ok": True, "report_id": report_id, "action": "approve",
                    "promote": promo, "case_count": ncase}
        if action == "reject":
            if not reason:
                return {"error": "驳回必须填写理由（reason），用于沉淀 badcase"}
            res = review_actions.reject_report(conn, report_id, reason=reason, reviewer=reviewer)
            if "error" in res:
                return res
            bad = review_actions.record_badcase(conn, report_id, title="报告驳回：" + reason)
            return {"ok": True, "report_id": report_id, "action": "reject",
                    "badcase": bad}
        return {"error": "action 必须为 approve 或 reject"}
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "json": "application/json; charset=utf-8",
                 "svg": "image/svg+xml", "ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path.startswith("/api/"):
            try:
                if path == "/api/customers":
                    self._send_json(api_customers(self.db_path, params))
                elif path == "/api/daily":
                    self._send_json(api_daily(self.db_path, params))
                elif path == "/api/base":
                    self._send_json(api_base(self.db_path, params))
                elif path == "/api/weeks":
                    self._send_json(api_weeks(self.db_path, params))
                elif path == "/api/report":
                    self._send_json(api_report(self.db_path, params))
                elif path == "/api/compare":
                    self._send_json(api_compare(self.db_path, params))
                elif path == "/api/cases":
                    self._send_json(api_cases(self.db_path, params))
                else:
                    self._send_json({"error": f"unknown api: {path}"}, 404)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        # 静态文件（中文文件名需 URL 解码）
        rel = unquote(path.lstrip("/"))
        if rel == "" or rel == "/":
            rel = "index.html"
        fpath = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not fpath.startswith(WEB_DIR):
            self._send_json({"error": "forbidden"}, 403)
            return
        self._send_file(fpath)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if path == "/api/ingest":
            try:
                self._send_json(api_ingest(self.db_path, body.decode("utf-8")))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/report":
            try:
                self._send_json(api_report_generate(self.db_path, body.decode("utf-8")))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/attrib":
            try:
                self._send_json(api_attrib(self.db_path, body.decode("utf-8")))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/review":
            try:
                self._send_json(api_review(self.db_path, body.decode("utf-8")))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        else:
            self._send_json({"error": f"unknown api: {path}"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser(description="诊断台 前端后端服务")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    Handler.db_path = args.db
    if not os.path.exists(args.db):
        sys.stderr.write(f"[warn] DB 不存在: {args.db}\n")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"诊断台前端已启动: http://{args.host}:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
