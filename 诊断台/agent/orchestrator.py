# -*- coding: utf-8 -*-
"""
诊断台 · 单 Agent 编排器（交接文档 9.3 主链 10 节点，自研轻量节点图，无 LangGraph）

节点:
  0 init            校验客户与周期（失败→终止）
  1 data_check      check_data_completeness（缺失严重→降级"数据不足"分支）
  2 full_scan       固定扫版位/计划/笔记/漏斗
  3 layer_diag      周环比+目标差距 → 每层 正常/轻微/显著/数据不足
  4 anomaly_rank    detect_anomalies + 权重 → Top3 / 观察项
  5 drill_down      先串联(get_funnel)后并联(split_by_dimension)，LLM 决定下钻顺序与深度   [LLM]
  6 case_retrieval  search_cases + LLM 差异判断（Badcase 不可引用）                       [LLM]
  7 suggest         绑定 8 条映射规则的建议生成                                          [LLM]
  8 verify          verify_evidence 事实校验（未过→回退/降级为假设）
  9 report_gen      assemble_report → 八章节 report_json → 人工审核

约束: 单次诊断 LLM 调用 ≤10 次；全量 tracing 落库 agent_step / agent_tool_call
用法:
  from orchestrator import ReviewOrchestrator
  o = ReviewOrchestrator("data/ad_review.db")
  result = o.run(customer_name="悦颜美容SPA", dry_run=True)   # dry_run: 跳过 LLM 节点
"""
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import tools
from tools import call_tool, _d

HERE = Path(__file__).parent
SYSTEM_PROMPT_PATH = HERE / "system_prompt.md"
LLM_CALL_BUDGET = 10


# ---------------------------------------------------------------- 周期推导
def derive_periods(conn, customer_id, anchor=None):
    """以全库最新数据日为锚（周复盘周期是日历周，与单个客户数据截止无关）：
    本周 = 最后一个完整自然周(周一~周日)，上周 = 其前一周
    注意：不可用该客户自身的 MAX(date) 做锚——数据缺失的客户会"自动跳过缺失周"，
    正好掩盖 E11 要拦截的数据不足。"""
    if anchor is None:
        anchor = conn.execute("SELECT MAX(date) FROM daily_metric").fetchone()[0]
    d = _d(anchor)
    cur_end = d - timedelta(days=(d.weekday() + 1) % 7 or 7)   # 回退到最近的周日
    cur_start = cur_end - timedelta(days=6)
    cmp_end = cur_start - timedelta(days=1)
    cmp_start = cmp_end - timedelta(days=6)
    f = lambda x: x.isoformat()
    return f(cur_start), f(cur_end), f(cmp_start), f(cmp_end)


class ReviewOrchestrator:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.task_id = None
        self.step_seq = 0
        self.tool_seq = 0
        self.llm_calls = 0
        self.llm_cost = 0.0
        self.fact_base = []          # 供 verify_evidence 的工具输出文本
        self.context = {}            # 节点间传递

    # ---------------- tracing 基础设施 ----------------
    def _new_task(self, customer_id, cs, ce, ps, pe, sim_version):
        cur = self.conn.execute(
            """INSERT INTO review_task(customer_id, task_type, cur_start, cur_end,
                                       cmp_start, cmp_end, trigger_type, status, sim_version)
               VALUES (?, 'weekly', ?, ?, ?, ?, 'manual', 'running', ?)""",
            (customer_id, cs, ce, ps, pe, sim_version))
        self.conn.commit()
        return cur.lastrowid

    def _step(self, name, status, input_summary=None, output_summary=None):
        self.step_seq += 1
        self.conn.execute(
            "INSERT INTO agent_step(task_id, seq, name, status, input_summary, output_summary) VALUES (?,?,?,?,?,?)",
            (self.task_id, self.step_seq, name, status,
             _s(input_summary), _s(output_summary)))
        self.conn.commit()
        return self.step_seq

    def _tool(self, step_seq, name, params, result, latency_ms, cost=0.0):
        self.tool_seq += 1
        ok = "error" not in (result or {})
        self.conn.execute(
            """INSERT INTO agent_tool_call(task_id, step_id, seq, tool_name, params_json,
                                           result_json, status, latency_ms, cost, error_msg)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (self.task_id, step_seq, self.tool_seq, name, _s(params),
             _s(result), "ok" if ok else "error", latency_ms, cost,
             result.get("error") if not ok else None))
        self.conn.commit()
        if ok:
            self.fact_base.append(_s(result))

    def _call(self, step_seq, name, **params):
        t0 = time.time()
        result = call_tool(self.conn, name, params)
        self._tool(step_seq, name, params, result, int((time.time() - t0) * 1000))
        return result

    def _llm(self, step_seq, messages, **kw):
        """预算内调用 DeepSeek；超限抛错（红线约束）"""
        import llm
        if self.llm_calls >= LLM_CALL_BUDGET:
            raise RuntimeError(f"LLM 调用超过预算 {LLM_CALL_BUDGET} 次（红线约束）")
        self.llm_calls += 1
        t0 = time.time()
        out = llm.call_deepseek(messages, **kw)
        self.llm_cost += out["cost_yuan"]
        self.conn.execute(
            """INSERT INTO agent_tool_call(task_id, step_id, seq, tool_name, params_json,
                                           result_json, status, latency_ms, cost)
               VALUES (?,?,?,?,?,?,?, ?, ?)""",
            (self.task_id, step_seq, self.tool_seq + self.llm_calls, "llm.deepseek",
             _s({"n_messages": len(messages)}), _s(out["text"])[:20000], "ok",
             int((time.time() - t0) * 1000), out["cost_yuan"]))
        self.conn.commit()
        return out["text"]

    # ---------------- 主链 ----------------
    def run(self, customer_name=None, customer_id=None, dry_run=False,
             metric_threshold=None, spend_threshold=None,
             cur_start=None, cur_end=None, cmp_start=None, cmp_end=None):
        # 节点0 初始化
        if customer_id is None:
            row = self.conn.execute("SELECT id FROM customer WHERE name=?", (customer_name,)).fetchone()
            if not row:
                return {"error": f"客户不存在: {customer_name}"}
            customer_id = row["id"]
        prof = self._probe(customer_id)
        if "error" in prof:
            return prof
        if cur_start and cur_end and cmp_start and cmp_end:
            cs, ce, ps, pe = cur_start, cur_end, cmp_start, cmp_end
        else:
            cs, ce, ps, pe = derive_periods(self.conn, customer_id)
        sim_version = self.conn.execute(
            "SELECT sim_version FROM daily_metric WHERE customer_id=? LIMIT 1", (customer_id,)).fetchone()[0]
        self.task_id = self._new_task(customer_id, cs, ce, ps, pe, sim_version)
        s0 = self._step("init", "running", input_summary={"customer_id": customer_id})
        self._step("init", "done", output_summary={"periods": [cs, ce, ps, pe], "sim_version": sim_version})
        self.context.update({"task_id": self.task_id, "cur_start": cs, "cur_end": ce,
                             "cmp_start": ps, "cmp_end": pe,
                             "generated_at": datetime.now().isoformat(timespec="seconds")})

        # 节点1 数据完整性
        s1 = self._step("data_check", "running")
        comp_chkl = self._call(s1, "check_data_completeness", customer_id=customer_id, start=cs, end=ce)
        prev_chkl = self._call(s1, "check_data_completeness", customer_id=customer_id, start=ps, end=pe)
        degraded = (not comp_chkl.get("sufficient")) or (not prev_chkl.get("sufficient"))
        self._step("data_check", "done",
                   output_summary={"cur": comp_chkl.get("verdict"), "prev": prev_chkl.get("verdict"),
                                   "degraded": degraded})
        self.context["data_check"] = {"cur": comp_chkl, "prev": prev_chkl, "degraded": degraded}

        # 节点2 全面扫描（固定，不可跳过）
        s2 = self._step("full_scan", "running")
        profile = self._call(s2, "get_customer_profile", customer_id=customer_id)
        comparison = self._call(s2, "get_period_comparison", customer_id=customer_id,
                                cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe)
        by_placement = self._call(s2, "get_period_comparison", customer_id=customer_id,
                                  cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe, dim="placement")
        funnel = self._call(s2, "get_funnel", customer_id=customer_id, start=cs, end=ce)
        infra = self._call(s2, "get_infrastructure", customer_id=customer_id,
                           cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe)
        self._step("full_scan", "done", output_summary={"layers": ["placement", "plan", "note", "funnel"]})
        self.context.update({"profile": profile, "comparison": comparison,
                             "by_placement": by_placement, "funnel": funnel, "infra": infra})

        # 节点3 分层诊断（含 14 天趋势线：目标成本指标日序列，供 LLM 区分一次性/持续波动）
        s3 = self._step("layer_diagnosis", "running")
        target_metric = "open_cost" if profile["optimize_target"] == "open" else "lead_cost"
        trend = self._call(s3, "get_trend", customer_id=customer_id, metric=target_metric, days=14, end=ce)
        self.context["trend"] = trend
        self.context["trend_metric"] = target_metric
        # 第 3 章双趋势：消耗趋势（单独取 spend 序列）
        spend_trend = self._call(s3, "get_trend", customer_id=customer_id, metric="spend", days=14, end=ce)
        self.context["trend_spend"] = spend_trend
        layers = self._layer_diagnosis(degraded)
        for lay in layers:
            lay["judgement"] = tools.norm_report_text(lay["judgement"])
            self.conn.execute(
                "INSERT INTO layer_diagnosis(task_id, layer,  status, judgement, evidence_json) VALUES (?,?,?,?,?)",
                (self.task_id, lay["layer"], lay["status"], lay["judgement"], _s(lay["evidence"])))
        self.conn.commit()
        self._step("layer_diagnosis", "done", output_summary={"layers": {l["layer"]: l["status"] for l in layers}})
        self.context["layer_diagnosis"] = layers

        # 节点4 异常识别排序
        s4 = self._step("anomaly_rank", "running")
        anomalies = self._call(s4, "detect_anomalies", customer_id=customer_id,
                               cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe,
                               metric_threshold=metric_threshold if metric_threshold is not None else 0.10,
                               spend_threshold=spend_threshold if spend_threshold is not None else 0.15)
        if degraded:
            # 数据不足分支（节点1 降级）：事件计算仅留痕，不落库、不进 Top3、不打硬结论
            anomalies = {"events": [], "n_events": 0,
                         "note": "数据不足分支：不输出异常排序"}
        for e in anomalies.get("events", []):
            self.conn.execute(
                """INSERT INTO anomaly(task_id, direction, form, location, weight_breakdown_json,
                                       impact_spend, impact_cost, magnitude, confidence, rank, is_top3, drill_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.task_id, e["direction"], e.get("form"), e["location"], _s(e["weight_breakdown"]),
                 e["weight_breakdown"]["spend_impact"], e["weight_breakdown"]["cost_contribution"],
                 e["weight_breakdown"]["magnitude"], e["weight_breakdown"]["confidence"],
                 e["rank"], int(e["is_top3"]), "pending"))
        self.conn.commit()
        self._step("anomaly_rank", "done", output_summary={"n_events": anomalies.get("n_events"),
                                                           "top3": [e["location"] for e in anomalies.get("events", []) if e["is_top3"]]})
        self.context["anomalies"] = anomalies

        if degraded:
            # 数据不足分支：不下钻、不打硬结论 → 直接轻量报告
            # 先区分"停投"与"缺数"（基建在投数与本期消耗均为 0 → 投放已停止）
            cur_spend = (self.context.get("comparison", {}).get("cur", {}) or {}).get("spend") or 0
            infra_cur = (self.context.get("infra", {}) or {}).get("cur", {}) or {}
            stopped = (cur_spend == 0 and not infra_cur.get("plans") and not infra_cur.get("notes"))
            if stopped:
                self.context["llm_summary"] = (
                    "本周数据不完整，且在投计划/笔记均为 0、消耗为 0——判断为投放已停止而非单纯缺数。"
                    "按红线不输出归因结论，建议先向客户/代理商确认停止原因，再决定重新起量还是本期不评。")
                self.context["llm_suggestions"] = [
                    {"text": "本期投放已停止（在投计划 0、在投笔记 0、消耗 0）。先向客户/代理商确认停止原因，"
                             "再决定重新起量还是本期不评；确认前不建议直接补数重跑复盘。",
                     "basis": "get_infrastructure + get_period_comparison（消耗=0）", "priority": "P1"}]
                self.context["llm_action_plan"] = [
                    {"action": "向客户/代理商确认本期投放停止的原因（主动停投/预算耗尽/账户问题）",
                     "date": self.context.get("generated_at", "")[:10],
                     "expect_metric": "拿到停止原因：若重新起量，下期复盘恢复完整对比；若停投，下期跳过该客户"}]
            else:
                self.context["llm_summary"] = "上周或本周数据不完整，按红线约束不输出归因结论，仅呈现数据缺口与已核实的指标现状。"
                self.context["llm_suggestions"] = [
                    {"text": "补齐缺失日期数据后重新发起复盘（完整性检查未通过，无法归因）", "basis": "check_data_completeness", "priority": "P0"}]
                self.context["llm_action_plan"] = [
                    {"action": "联系数据对接方补齐缺失日期数据，补齐后重新触发周度复盘",
                     "date": self.context.get("generated_at", "")[:10],
                     "expect_metric": "数据完整性检查通过，复盘报告恢复归因与建议章节"}]
            self.context["llm_top3_detail"] = []
            s5 = self._step("drill_down", "skipped", output_summary="数据不足分支：跳过下钻")
            # 数据不足分支仍保留"同赛道历史打法"参考：案例属行业经验参考，非归因结论，不违反红线
            s6 = self._step("case_retrieval", "running")
            _prof = self.context.get("profile") or {}
            _cases = self._call(s6, "search_cases", industry=_prof.get("industry"),
                                sector=_prof.get("sector"), signature_terms=[])
            self.context["cases"] = _cases
            if _cases.get("cases"):
                _crefs = [{"case_id": c["id"], "industry": c.get("industry"), "sector": c.get("sector"),
                           "anomaly_signature": c.get("anomaly_signature"),
                           "action_taken": c.get("action_taken"), "result_after": c.get("result_after"),
                           "similarity_points": "", "key_differences": "", "adopted": 1}
                          for c in _cases["cases"]]
                self.context["case_refs"] = _crefs
                self._step("case_retrieval", "done",
                           output_summary={"n": len(_crefs), "degraded": True})
            else:
                self._step("case_retrieval", "skipped", output_summary="无同赛道可引用案例")
            s7 = self._step("suggest", "skipped",
                            output_summary="数据不足分支：" + ("投放已停止→建议确认原因" if stopped else "建议仅限补数据"))
            report = self._finish()
            return report

        # 节点5 异常下钻：先串联后并联；LLM 决定顺序与深度
        s5 = self._step("drill_down", "running")
        if dry_run:
            self._step("drill_down", "skipped", output_summary="dry_run：跳过 LLM 下钻")
        else:
            self._drill_with_llm(s5, customer_id, cs, ce, ps, pe)

        # 节点6 案例检索比较
        s6 = self._step("case_retrieval", "running")
        sig = [e["metric"] for e in anomalies.get("events", []) if e["is_top3"]][:3]
        cases = self._call(s6, "search_cases", industry=profile["industry"],
                           sector=profile["sector"], signature_terms=sig)
        self.context["cases"] = cases
        if dry_run or not cases["cases"]:
            self._step("case_retrieval", "done" if not cases["cases"] else "skipped",
                       output_summary={"n": cases["n"]})
        else:
            txt = self._llm(s6, self._case_messages(profile, anomalies, cases), json_mode=True)
            self.context["llm_case_compare"] = txt
            # E15: 引用案例留痕（相似点/关键差异优先取 LLM 结构化字段，否则原文兜底）
            cc = _parse_json(txt, {})
            case_refs = []
            for c in cases["cases"]:
                case_refs.append({
                    "case_id": c["id"], "industry": c.get("industry"), "sector": c.get("sector"),
                    "anomaly_signature": c.get("anomaly_signature"),
                    "action_taken": c.get("action_taken"), "result_after": c.get("result_after"),
                    "similarity_points": cc.get("similarity_points") if isinstance(cc, dict) else txt,
                    "key_differences": cc.get("key_differences") if isinstance(cc, dict) else txt,
                    "adopted": 1})
                self.conn.execute(
                    """INSERT INTO case_ref_log(task_id, case_id, similarity_points, key_differences, adopted)
                       VALUES (?,?,?,?,1)""",
                    (self.task_id, c["id"],
                     str(cc.get("similarity_points") or txt)[:2000],
                     str(cc.get("key_differences") or txt)[:2000]))
            self.conn.commit()
            self.context["case_refs"] = case_refs  # 第6章透出相似点/关键差异
            self._step("case_retrieval", "done", output_summary={"llm": "差异判断完成",
                                                                  "case_ref_log": len(cases["cases"])})

        # 节点7 建议生成（绑定 8 条映射规则）
        s7 = self._step("suggest", "running")
        if dry_run:
            self.context["llm_suggestions"] = self._rule_suggestions(anomalies, profile)
            self.context["llm_action_plan"] = []
            self._step("suggest", "done", output_summary="dry_run：规则模板建议")
        else:
            txt = self._llm(s7, self._suggest_messages(profile, anomalies), json_mode=True)
            sg = _parse_json(txt, {})
            if isinstance(sg, list):
                self.context["llm_suggestions"] = sg
                self.context["llm_action_plan"] = []
            elif isinstance(sg, dict):
                self.context["llm_suggestions"] = sg.get("suggestions", [])
                self.context["llm_action_plan"] = sg.get("action_plan", [])
            else:
                self.context["llm_suggestions"] = []
                self.context["llm_action_plan"] = []
            self._sanitize_suggest(anomalies)
            self._step("suggest", "done", output_summary={"llm": "建议生成完成"})

        # 节点8 证据校验
        s8 = self._step("verify", "running")
        claims = []
        for sg in self.context.get("llm_suggestions", []):
            if isinstance(sg, dict):
                claims.append({"text": sg.get("text", ""), "kind": "suggestion", "evidence": []})
        verify = call_tool(self.conn, "verify_evidence", {"claims": claims, "fact_base": self.fact_base})
        self._tool(s8, "verify_evidence", {"n_claims": len(claims)}, verify, 0)
        self._step("verify", "done", output_summary={"all_facts_verified": verify.get("all_facts_verified")})
        self.context["verify"] = verify

        # 节点9 报告生成
        return self._finish()

    # ---------------- 子流程 ----------------
    def _probe(self, customer_id):
        r = self.conn.execute("SELECT id FROM customer WHERE id=?", (customer_id,)).fetchone()
        return {"error": "客户不存在"} if not r else {"ok": True}

    def _layer_diagnosis(self, degraded):
        comp, plc, infra, fun = (self.context["comparison"], self.context["by_placement"],
                                 self.context["infra"], self.context["funnel"])
        out = []

        def sev(change, t=0.15):
            if change is None:
                return "数据不足"
            if abs(change) >= 0.3:
                return "显著"
            if abs(change) >= t:
                return "轻微"
            return "正常"

        def _chg_txt(v):
            """环比文本：None→—，0→持平（措辞红线），其余 ±x.x%"""
            if v is None:
                return "—"
            if v == 0:
                return "持平"
            return f"{v*100:+.1f}%"

        chg = comp["metrics_change"]
        rate_cn = [("CTR", "CTR"), ("button_rate", "按钮率"), ("open_rate", "开口率"), ("lead_rate", "留资率")]
        rate_ev = {m: chg.get(m) for m, _ in rate_cn}
        rate_txt = "、".join(f"{cn} {_chg_txt(rate_ev.get(m))}" for m, cn in rate_cn)
        out.append({"layer": "funnel",
                    "status": "数据不足" if degraded else max((sev(v) for v in rate_ev.values()),
                                                              key=lambda s: ["正常", "轻微", "数据不足", "显著"].index(s)),
                    "judgement": f"漏斗各环节率环比：{rate_txt}", "evidence": rate_ev})
        plc_sev = "正常"
        for row in plc["rows"]:
            c = row["metrics_change"].get("CPM")
            if c is not None and abs(c) >= 0.2:
                plc_sev = "显著" if abs(c) >= 0.4 else "轻微"
        dim_cn = {"feed": "信息流", "search": "搜索"}
        plc_ev = {r["dim"]: r["metrics_change"].get("CPM") for r in plc["rows"]}
        plc_txt = "、".join(f"{dim_cn.get(d, d)} CPM {_chg_txt(v)}" for d, v in plc_ev.items()) or "无版位数据"
        out.append({"layer": "placement", "status": "数据不足" if degraded else plc_sev,
                    "judgement": f"版位 CPM 环比：{plc_txt}", "evidence": plc_ev})

        def _gate_txt(g, unit):
            rel = f"{g['rel']*100:+.1f}%" if g.get("rel") is not None else "环比不可算"
            hit = "，触发基建异动门槛（变化≥2 且 ≥20%）" if g["hit"] else ""
            return f"{g['prev']}→{g['cur']}（{g['delta']:+d}{unit} / {rel}）{hit}"

        pg, ng = infra["plan_gate"], infra["note_gate"]
        out.append({"layer": "plan", "status": "数据不足" if degraded else ("显著" if (pg["hit"] or ng["hit"]) else "正常"),
                    "judgement": f"在投计划数 {_gate_txt(pg, '条')}；本期新建计划 {infra['cur']['new_plans']} 条", "evidence": pg})
        out.append({"layer": "note", "status": "数据不足" if degraded else ("显著" if ng["hit"] else "正常"),
                    "judgement": f"在投笔记数 {_gate_txt(ng, '篇')}；本期新建笔记 {infra['cur']['new_notes']} 篇", "evidence": ng})
        return out

    def _drill_with_llm(self, step_seq, customer_id, cs, ce, ps, pe):
        """先串联后并联：漏斗定位 → 版位/计划/笔记贡献拆分（代码），LLM 解释与决定是否继续深挖"""
        anomalies = self.context["anomalies"]
        profile = self.context["profile"]
        top3 = [e for e in anomalies.get("events", []) if e["is_top3"]]
        # 串联：漏斗环节定位（代码先给证据）
        funnel = self._call(step_seq, "get_funnel", customer_id=customer_id, start=cs, end=ce)
        # 并联：贡献拆分（正负向共用同一引擎与顺序 版位→计划→笔记）
        splits = {}
        for dim in ("placement", "plan", "note"):
            splits[dim] = self._call(step_seq, "split_by_dimension", customer_id=customer_id,
                                     cur_start=cs, cur_end=ce, cmp_start=ps, cmp_end=pe,
                                     metric="spend", dim=dim)
        # 新旧对比（涉及基建/新笔记质量时）
        newold = self._call(step_seq, "compare_new_old", customer_id=customer_id, start=cs, end=ce, obj_type="note")
        self.context["drill"] = {"funnel": funnel, "splits": splits, "new_old": newold}
        # LLM：解释 + 指定下一步深挖对象
        positive = bool(top3) and all(e["direction"] == "positive" for e in top3) and \
            not any(e.get("is_adverse") for e in top3)
        msgs = [{"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": json.dumps({
                    "task": "下钻解释", "mode": "positive" if positive else "negative",
                    "profile": profile, "top3": top3, "funnel": funnel,
                    "splits": splits, "new_old": newold,
                    "trend_14d": self.context.get("trend"),
                    "output_format": {
                        "summary": "核心结论摘要（1-2句，含关键数字）",
                        "top3_events": [{"rank": 1, "location": "事件位置", "reason": "归因解释",
                                         "evidence": ["依据1", "依据2"], "confidence": "高/中/低"}],
                        "drill_next": [{"obj_type": "note/plan/placement", "obj_id": None}]}},
                 ensure_ascii=False, default=str)}]
        txt = self._llm(step_seq, msgs, json_mode=True)
        plan = _parse_json(txt, {})
        self.context["llm_drill"] = plan
        diag = plan.get("diagnosis") if isinstance(plan, dict) else None
        diag = diag if isinstance(diag, dict) else {}
        self.context["llm_summary"] = diag.get("summary") or plan.get("summary") or plan.get("conclusion") or "（下钻未生成摘要）"
        self.context["llm_top3_detail"] = [tools.norm_top3(x) for x in
                                            (diag.get("top3_events") or plan.get("top3_events") or plan.get("top3_detail") or [])]
        # LLM 指定的深挖对象执行 drill_down_object（每个 ≤1 次）
        for d in (diag.get("drill_next") or plan.get("drill_next") or [])[:2]:
            res = self._call(step_seq, "drill_down_object", obj_type=d.get("obj_type", "note"),
                             obj_id=d.get("obj_id"), start=cs, end=ce)
        self._step("drill_down", "done", output_summary={"llm_plan": (diag.get("summary") or "")[:200]})

    def _case_messages(self, profile, anomalies, cases):
        comp = self.context.get("comparison", {})
        return [{"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": json.dumps({
                    "task": "案例比较", "profile": profile,
                    "anomalies": [e for e in anomalies.get("events", []) if e["is_top3"]],
                    "cur_metrics": comp.get("metrics_cur"),
                    "metrics_change": comp.get("metrics_change"),
                    "cases": cases["cases"],
                    "output_format": {"similarity_points": "与当前客户的相似点（引用具体数字）",
                                      "key_differences": "关键差异（引用具体数字；当前客户当期指标已在 cur_metrics/metrics_change 中给出，禁止写'数据未提供'）",
                                      "adopted": True}}, ensure_ascii=False, default=str)}]

    def _suggest_messages(self, profile, anomalies):
        return [{"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": json.dumps({
                    "task": "建议生成", "profile": profile,
                    "anomalies": anomalies.get("events", []),
                    "drill": self.context.get("drill"),
                    "report_generated_at": self.context.get("generated_at"),
                    "output_format": {"suggestions": ["text/basis/priority/risk/watch_metric"],
                                      "action_plan": ["action/date/expect_metric（无 owner 字段；date 不早于 report_generated_at）"]}},
                    ensure_ascii=False, default=str)}]

    def _sanitize_suggest(self, anomalies):
        """节点7产出护栏（确定性代码兜底，与 prompt 第7节红线配套）：
        - 无异常事件（整体状态=正常）→ 建议与行动计划清空
        - 无不利 Top3（整体状态=需关注）→ P0 一律降级 P1
        - action_plan 删除 owner 字段；日期早于生成日的锚定为生成日
        """
        events = anomalies.get("events", [])
        if not events:
            self.context["llm_suggestions"] = []
            self.context["llm_action_plan"] = []
            return
        adverse_top3 = any(e.get("is_adverse") for e in events if e.get("is_top3"))
        for sg in self.context.get("llm_suggestions", []):
            if isinstance(sg, dict):
                p = sg.get("priority")
                if p == "P0" and not adverse_top3:
                    sg["priority"] = "P1"
                elif p not in ("P0", "P1"):
                    sg["priority"] = "P1"
        gen = _d(self.context["generated_at"][:10])
        ap = []
        for a in self.context.get("llm_action_plan", []):
            if not isinstance(a, dict):
                continue
            a.pop("owner", None)
            d = _parse_date(a.get("date"))
            if d is not None and d < gen:
                a["date"] = gen.isoformat()
            ap.append(a)
        self.context["llm_action_plan"] = ap

    def _rule_suggestions(self, anomalies, profile):
        """8 条映射规则底座（dry_run / LLM 兜底）——内容与 system_prompt.md 第 3.5 节一致，改一边必须同步另一边"""
        mapping = {
            "spend": "消耗下降→查基建（在投计划/笔记是否减少）→建议补量",
            "CPM": "CPM上涨→查版位结构变化（pp）→调整预算结构",
            "CTR": "CTR下降→查标题与素材形式（封面归因仅作待验证假设）",
            "button_rate": "按钮点击率下降→查内容承接、新旧笔记差异",
            "open_rate": "私信开口率下降→查私信入口与首响",
            "lead_rate": "链路留资率下降→查话术与回复速度，建议与客户确认",
            "lead_cost": "目标成本上涨→从成本反向拆最大贡献环节",
            "open_cost": "目标成本上涨→从成本反向拆最大贡献环节",
        }
        out = []
        for e in anomalies.get("events", []):
            m = e["metric"]
            if m in mapping and e.get("is_adverse"):
                out.append({"text": mapping[m], "basis": f"{e['location']} 环比 {e['change']:.1%}",
                            "priority": "P0" if e["is_top3"] else "P1", "risk": "", "watch_metric": m})
            elif m in ("spend",) and e["direction"] == "positive":
                out.append({"text": "成本下降且消耗上升（正向）→提炼可复制组合+小幅扩量+成本回退阈值止损",
                            "basis": e["location"], "priority": "P1", "risk": "扩量后成本回退超阈值即止损", "watch_metric": "lead_cost"})
        return out

    def _finish(self):
        s9 = self._step("report_gen", "running")
        report = call_tool(self.conn, "assemble_report", self.context)
        self._tool(s9, "assemble_report", {"context_keys": list(self.context.keys())}, report, 0)
        self.conn.execute(
            """INSERT INTO report(task_id, version, status, schema_version, report_json, sim_version)
               VALUES (?,?, 'draft', ?, ?, ?)""",
            (self.task_id, 1, report["schema_version"], _s(report), self.context.get("sim_version", "sim-v1.0.0")))
        self.conn.execute(
            "UPDATE review_task SET status='succeeded', total_cost=?, finished_at=datetime('now','localtime') WHERE id=?",
            (self.llm_cost, self.task_id))
        self.conn.commit()
        self._step("report_gen", "done", output_summary={"overall": report.get("overall_status")})
        report["task_id"] = self.task_id
        report["llm_calls"] = self.llm_calls
        report["llm_cost_yuan"] = round(self.llm_cost, 6)
        return report

    def system_prompt(self):
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    def close(self):
        self.conn.close()


def _s(x):
    try:
        return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _parse_date(s):
    """宽松解析 YYYY-MM-DD / M月D日 / YYYY/M/D，失败返回 None"""
    import re
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})月(\d{1,2})日", s)
    if m:
        y = datetime.now().year
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _parse_json(txt, default):
    try:
        return json.loads(txt)
    except Exception:
        a, b = txt.find("{"), txt.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(txt[a:b + 1])
            except Exception:
                pass
        return default
