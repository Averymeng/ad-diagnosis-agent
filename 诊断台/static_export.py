# -*- coding: utf-8 -*-
"""导出诊断台为静态快照 web/snapshot.json，供无后端的前端只读模式使用。

零成本：仅读取 SQLite 并复用后端纯计算函数（attrib_reason / api_daily / api_base），
不调用任何 LLM。生成的 snapshot.json 让前端在检测不到后端 API 时自动回退，
从而可以用 CloudStudio 静态托管等方式发布成"永不掉线的只读大盘"。

用法：python3 static_export.py
"""
import json
import os
import sqlite3
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "agent"))

import api_server as api  # 复用 api_daily / api_customers / api_attrib / api_base

DB = os.path.join(HERE, "data", "ad_review.db")
OUT = os.path.join(HERE, "web", "snapshot.json")


def build_snapshot(db_path=DB):
    """从数据库实时构建快照 dict（与导出的 snapshot.json 同构）。
    供静态导出与后端 /api/snapshot 复用；零 LLM 调用。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError("未找到数据库 " + db_path)

    # 1) 客户列表（light）
    customers = api.api_customers(db_path, {"light": ["1"]})

    # 2) 日度序列（前端任意窗口计算的基础）
    daily_resp = api.api_daily(db_path, {})
    daily = daily_resp["customers"]
    maxd = daily_resp.get("maxd", "")

    # 3) 默认窗口 = 最新自然周（取 maxd 所在周一为起点）
    md = datetime.date.fromisoformat(maxd) if maxd else datetime.date.today()
    ws = md - datetime.timedelta(days=md.weekday())  # 周一
    cs = ws.isoformat()
    ce = (ws + datetime.timedelta(days=6)).isoformat()
    ps = (ws - datetime.timedelta(days=7)).isoformat()
    pe = (ws - datetime.timedelta(days=1)).isoformat()

    # 4) 确定性归因（全部客户，无 LLM）
    ids = [c["id"] for c in customers]
    attrib = {}
    try:
        ar = api.api_attrib(
            db_path,
            json.dumps(
                {"ids": ids, "cur_start": cs, "cur_end": ce,
                 "cmp_start": ps, "cmp_end": pe}
            ),
        )
        attrib = ar.get("reasons", {})
    except Exception as e:
        print("警告：归因生成失败，将使用前端兜底文案：", e)
        attrib = {}

    # 5) 基建（在投/新投 笔记·计划，默认窗口）
    try:
        base = api.api_base(
            db_path,
            {"start": [cs], "end": [ce], "prev_start": [ps], "prev_end": [pe]},
        )
    except Exception:
        base = None

    # 6) 报告：每个客户取最新一份（report_json 原样存，前端 renderReportModal 复用）
    reports = {}
    conn = api.get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT rt.customer_id, r.report_json FROM report r
               JOIN review_task rt ON rt.id=r.task_id
               WHERE r.report_json IS NOT NULL
               ORDER BY r.id DESC"""
        ).fetchall()
        seen = set()
        for r in rows:
            cid = r["customer_id"]
            if cid in seen:
                continue
            seen.add(cid)
            try:
                reports[str(cid)] = json.loads(r["report_json"])
            except Exception:
                pass

        # 7) 案例库（只读，供销售工作台打法匹配；不含任何写入）
        cases = []
        for c in conn.execute(
            """SELECT dc.id, dc.anomaly_signature, dc.action_taken, dc.result_after,
                      dc.referenceable, dc.status, dc.optimize_target,
                      i.name AS industry, s.name AS sector
               FROM diag_case dc
               LEFT JOIN industry i ON i.id=dc.industry_id
               LEFT JOIN sector s ON s.id=dc.sector_id"""
        ).fetchall():
            cases.append({
                "id": c["id"],
                "sector": c["sector"],
                "industry": c["industry"],
                "anomaly_signature": c["anomaly_signature"],
                "action_taken": c["action_taken"],
                "result_after": c["result_after"],
                "referenceable": c["referenceable"],
                "status": c["status"],
                "optimize_target": c["optimize_target"],
            })
    finally:
        conn.close()

    snap = {
        "meta": {
            "maxd": maxd,
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "offline": True,
        },
        "customers": customers,
        "daily": daily,
        "attrib": attrib,
        "base": base,
        "reports": reports,
        "cases": cases,
    }
    return snap


def main():
    if not os.path.exists(DB):
        print("错误：未找到数据库", DB)
        sys.exit(1)

    snap = build_snapshot(DB)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    print("已导出", OUT)
    print(
        "  客户 %d 个 · 日度序列 %d 个客户 · 归因 %d 条 · 报告 %d 份 · 案例 %d 条"
        % (len(snap["customers"]), len(snap["daily"]), len(snap["attrib"]),
           len(snap["reports"]), len(snap["cases"]))
    )


if __name__ == "__main__":
    main()
