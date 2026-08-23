#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""案例库挖矿：从已有诊断报告自动起草候选案例，输出审核表（方案②）。

流程：
  1. 读数据库里所有已生成的报告（report 表 -> review_task -> customer）。
  2. 对每份"有异常"的报告，抽取 异常签名 + 关键证据 + 策略建议。
  3. 按 diag_case 现有 schema 起草候选案例。
  4. 与现有案例去重（签名词元重叠则跳过）。
  5. 输出 求职材料/案例审核表.md，按行业分组，每条标 [待审核]。

用户最小动作：把 [待审核] 改成 [通过] / 删掉不行的，告诉我即可。
之后由 --insert 把 [通过] 的写入 diag_case 并重跑评测确认 126/126 仍全绿。

用法：
  python3 诊断台/eval/mine_cases.py            # 生成 案例审核表.md
  python3 诊断台/eval/mine_cases.py --insert   # 把审核表中 [通过] 的写入 diag_case
"""
import os
import re
import json
import sqlite3
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "诊断台", "data", "ad_review.db")
REVIEW_MD = os.path.join(ROOT, "求职材料", "案例审核表.md")


def conn_db():
    if not os.path.exists(DB):
        raise SystemExit(f"找不到数据库: {DB}")
    cc = sqlite3.connect(DB)
    cc.row_factory = sqlite3.Row
    return cc


def get_customer_dims(c, customer_id):
    cols = [d[0] for d in c.execute("SELECT * FROM customer LIMIT 0").description]
    row = c.execute(f"SELECT * FROM customer WHERE id=?", (customer_id,)).fetchone()
    if not row:
        return {}
    d = dict(zip(cols, row))
    return {
        "industry_id": d.get("industry_id") or 0,
        "sector_id": d.get("sector_id") or 0,
        "category_id": d.get("category_id") or 0,
        "optimize_target": d.get("optimize_target") or "lead",
    }


def clean(s):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s


def join_anomaly(items):
    """top3_detail 可能是 [短语,...] 或 [单字,...]（逐字拆开的垃圾）。
    单字就直接拼接成句，短语就换行拼。"""
    if not items:
        return ""
    if all(isinstance(x, str) and len(x) <= 1 for x in items):
        return "".join(items).strip()
    return "\n".join(str(x).strip() for x in items if str(x).strip())


def extract_evidence(text):
    ev = {}
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z_]{1,8})\s*[（(]?\s*([±+]?\d+(?:\.\d+)?\s*%?)", text):
        ev[m.group(1)] = m.group(2)
    return ev


def fetch_reports(c):
    """取每份报告及其 customer_id / customer_name。"""
    cols = [d[0] for d in c.execute("SELECT * FROM report LIMIT 0").description]
    out = []
    if "task_id" in cols:
        rows = c.execute(
            "SELECT r.report_json, t.customer_id FROM report r "
            "LEFT JOIN review_task t ON r.task_id=t.id").fetchall()
    else:
        rows = c.execute("SELECT report_json, customer_id FROM report").fetchall()
    name_map = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM customer")}
    for report_json, cid in rows:
        try:
            j = json.loads(report_json) if isinstance(report_json, str) else report_json
        except Exception:
            continue
        out.append((cid, name_map.get(cid, ""), j))
    return out


def existing_signatures(c):
    try:
        rows = c.execute("SELECT anomaly_signature FROM diag_case").fetchall()
        sigs = [clean(r[0]) for r in rows if r[0]]
    except sqlite3.Error:
        sigs = []
    return sigs


def token_overlap(a, b):
    ta = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]+", a))
    tb = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]+", b))
    return len(ta & tb) / max(1, len(ta))


def get_strategy(chapters):
    parts = []
    s7 = chapters.get("7_优化建议") or []
    if isinstance(s7, list):
        for x in s7:
            if isinstance(x, dict):
                parts.append(clean(x.get("text", "")))
            elif isinstance(x, str):
                parts.append(clean(x))
    s8 = chapters.get("8_行动计划") or []
    if isinstance(s8, list):
        for x in s8:
            if isinstance(x, dict):
                parts.append(clean(x.get("action", "")))
            elif isinstance(x, str):
                parts.append(clean(x))
    return "\n".join(p for p in parts if p)


def build_candidates():
    c = conn_db()
    reports = fetch_reports(c)
    dims_cache = {}
    sigs = existing_signatures(c)
    candidates = []
    seen_sig = set()
    for cid, cname, j in reports:
        if not isinstance(j, dict):
            continue
        chapters = j.get("chapters", {})
        a = chapters.get("5_异常与原因") or {}
        if isinstance(a, dict):
            anomaly = join_anomaly(a.get("top3_detail") or []) or join_anomaly(a.get("watchlist") or [])
        else:
            anomaly = clean(a)
        if not anomaly:
            continue
        if re.search(r"无明显异常|无异常|未见异常|正常", anomaly):
            continue
        strategy = get_strategy(chapters)
        if cid not in dims_cache:
            dims_cache[cid] = get_customer_dims(c, cid)
        dims = dims_cache[cid]
        signature = clean(anomaly)[:80]
        if signature in seen_sig:
            continue
        seen_sig.add(signature)
        # 去重：与现有案例签名重叠 > 0.5 视为已覆盖
        if any(token_overlap(signature, s) > 0.5 for s in sigs):
            continue
        ev = extract_evidence(anomaly + " " + strategy)
        cand = {
            "industry_id": dims.get("industry_id", 0),
            "sector_id": dims.get("sector_id", 0),
            "category_id": dims.get("category_id", 0),
            "optimize_target": dims.get("optimize_target", "lead"),
            "anomaly_signature": signature,
            "key_evidence_json": ev or {},
            "action_taken": clean(strategy)[:300] or "（待补：来自策略章节）",
            "result_after": "（审核时补充实际效果）",
            "status": "reference",
            "referenceable": 1,
            "_src": f"{cname}(#{cid})",
        }
        candidates.append(cand)
    c.close()
    return candidates


def write_review_md(candidates):
    os.makedirs(os.path.dirname(REVIEW_MD), exist_ok=True)
    lines = ["# 案例库审核表（挖矿候选）", "",
             f"> 共 {len(candidates)} 条候选，来自已有诊断报告（剔除无明显异常与已有覆盖）。",
             "> 审核：把标题 `[待审核]` 改为 `[通过]` 即保留；划掉或删除即丢弃。",
             "> 改完告诉我，我执行 `--insert` 写入 diag_case，并重跑评测确认 126/126 全绿。", ""]
    for i, cd in enumerate(candidates, 1):
        lines += [
            f"## [{i}] [待审核] {cd['anomaly_signature']}",
            f"- 来源报告：{cd['_src']}",
            f"- 维度：industry_id={cd['industry_id']} sector_id={cd['sector_id']} category_id={cd['category_id']} target={cd['optimize_target']}",
            f"- 关键证据：{json.dumps(cd['key_evidence_json'], ensure_ascii=False)}",
            f"- 采取动作：{cd['action_taken']}",
            f"- 实际效果：{cd['result_after']}",
            "",
        ]
    with open(REVIEW_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return REVIEW_MD


def insert_approved():
    if not os.path.exists(REVIEW_MD):
        raise SystemExit("先生成审核表")
    text = open(REVIEW_MD, encoding="utf-8").read()
    blocks = re.split(r"^## ", text, flags=re.M)
    c = conn_db()
    n = 0
    for b in blocks[1:]:
        if not b.strip().startswith("[通过]"):
            continue
        sig = b.split("\n", 1)[0].replace("[通过]", "").strip()
        m = re.search(r"industry_id=(\d+).*?sector_id=(\d+).*?category_id=(\d+).*?target=(\w+)", b)
        if not m:
            continue
        industry_id, sector_id, category_id, target = (int(m.group(1)), int(m.group(2)),
                                                        int(m.group(3)), m.group(4))
        ev = re.search(r"关键证据：(\{.*?\})", b)
        act = re.search(r"采取动作：(.*)", b)
        res = re.search(r"实际效果：(.*)", b)
        c.execute(
            "INSERT INTO diag_case(industry_id, sector_id, category_id, optimize_target, "
            "anomaly_signature, key_evidence_json, action_taken, result_after, status, referenceable) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (industry_id, sector_id, category_id, target, sig,
             ev.group(1) if ev else "{}",
             act.group(1).strip() if act else "",
             res.group(1).strip() if res else "", "reference", 1))
        n += 1
    c.commit()
    c.close()
    print(f"已写入 {n} 条案例。下一步请跑评测确认 126/126 全绿。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--insert", action="store_true", help="把审核表中 [通过] 的写入 diag_case")
    args = ap.parse_args()
    if args.insert:
        insert_approved()
        return
    cds = build_candidates()
    path = write_review_md(cds)
    print(f"候选案例 {len(cds)} 条 -> {path}")


if __name__ == "__main__":
    main()
