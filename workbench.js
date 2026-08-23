/* 诊断台 · 客户经营（销售工作台子模块；前端只读分析层，数据源为 snapshot.json，不调用任何后端写入）
 *
 * 设计原则（与诊断引擎隔离）：
 *  - 只读 SNAP.customers / SNAP.reports / SNAP.cases，不修改任何数据库。
 *  - 健康分、分群、打法推荐、覆盖度自检均为前端纯计算，可离线运行（CloudStudio/GitHub Pages）。
 *  - 打法推荐复用案例库（diag_case）作为同赛道历史打法来源，与 RAG 召回同源。
 */

const WB_TIER = {
  "健康": { color: "#32D583", bg: "rgba(50,213,131,.12)" },
  "关注": { color: "#FDB022", bg: "rgba(253,176,34,.14)" },
  "预警": { color: "#F97066", bg: "rgba(249,112,102,.14)" },
  "未知": { color: "#94A3B8", bg: "rgba(148,163,184,.14)" },
};

/* 健康分公式（可解释，便于面试辩护；满分 100）：
 *   ① 消耗动能：近 7 天 vs 前 7 天消耗，下滑按比例扣分（上限 25），增长小幅加分（上限 5）
 *   ② 指标惩罚：对 9 个核心指标按“方向是否正确”扣分，单项上限 12，合计上限 35
 *   ③ 状态修正：需关注 −10 / 需行动 −22
 *   ④ 异常惩罚：top3 异常每条 3 分 + watchlist 每条 2 分，合计上限 10
 *   最终 score 夹在 [0,100]；分群：>=80 健康 / 60~79 关注 / <60 预警
 */
function computeHealth(rep, name) {
  if (!rep) return { score: null, tier: "未知", base: 100, mPen: 0, aPen: 0, sPen: 0, stPen: 0, factors: [], warnings: [] };
  let sc = 100;
  const factors = [];

  // ① 消耗动能
  let sPen = 0;
  const ser = (SNAP.daily && SNAP.daily[name]) || [];
  if (ser.length >= 14) {
    const ss = ser.slice().sort((a, b) => a.date < b.date ? -1 : 1);
    const cur = ss.slice(-7).reduce((s, r) => s + (r.spend || 0), 0);
    const prev = ss.slice(-14, -7).reduce((s, r) => s + (r.spend || 0), 0);
    if (prev > 0) {
      const d = (cur - prev) / prev;
      if (d < 0) { sPen = Math.min(25, Math.abs(d) * 50); sc -= sPen; factors.push({ label: "消耗环比", val: (d * 100).toFixed(0) + "%", impact: -Math.round(sPen) }); }
      else if (d > 0) { const b = Math.min(5, d * 10); sc += b; factors.push({ label: "消耗环比", val: "+" + (d * 100).toFixed(0) + "%", impact: Math.round(b) }); }
    }
  }

  // ② 指标惩罚
  const ch3 = (rep.chapters && rep.chapters["3_指标与趋势"]) || {};
  const mc = ch3.metrics_change || {};
  const GOOD_UP = { CTR: 0.4, button_rate: 0.4, open_rate: 0.5, lead_rate: 0.6, lead_cvr: 0.8 };
  const GOOD_DOWN = { CPM: 0.25, CPC: 0.25, open_cost: 0.6, lead_cost: 0.7 };
  let mPen = 0;
  for (const k in GOOD_UP) {
    const d = mc[k] || 0;
    if (d < 0) { const p = Math.min(12, Math.abs(d) * 100 * GOOD_UP[k]); mPen += p; factors.push({ label: (METRIC_CN[k] || k) + " 下降", val: (d * 100).toFixed(1) + "%", impact: -Math.round(p) }); }
  }
  for (const k in GOOD_DOWN) {
    const d = mc[k] || 0;
    if (d > 0) { const p = Math.min(12, d * 100 * GOOD_DOWN[k]); mPen += p; factors.push({ label: (METRIC_CN[k] || k) + " 上升", val: "+" + (d * 100).toFixed(1) + "%", impact: -Math.round(p) }); }
  }
  mPen = Math.min(35, mPen); sc -= mPen;

  // ③ 状态修正
  const status = rep.overall_status || "正常";
  let stPen = 0;
  if (status === "需关注") { stPen = 10; sc -= 10; }
  else if (status === "需行动") { stPen = 22; sc -= 22; }

  // ④ 异常惩罚
  const ch5 = (rep.chapters && rep.chapters["5_异常与原因"]) || {};
  const top3 = ch5.top3_detail || [];
  const watch = ch5.watchlist || [];
  const aPen = Math.min(10, top3.length * 3 + watch.length * 2);
  sc -= aPen;

  sc = Math.max(0, Math.min(100, Math.round(sc)));

  const warnings = [];
  if (sc < 60) warnings.push("健康分偏低（" + sc + "）");
  if ((mc.lead_cost || 0) > 0.05) warnings.push("留资成本上升 " + Math.round(mc.lead_cost * 100) + "%");
  const reasonTxt = top3.map(a => a.reason || "").join(" ");
  if (reasonTxt.indexOf("消耗下降") >= 0) warnings.push("消耗环比下滑");
  if (status === "需行动") warnings.push("系统判定需行动");

  const tier = sc >= 80 ? "健康" : (sc >= 60 ? "关注" : "预警");
  return { score: sc, tier, base: 100, mPen: Math.round(mPen), aPen: Math.round(aPen), sPen: Math.round(sPen), stPen, factors, warnings };
}

/* 基于异常 location / reason 的规则化打法推荐 */
function rulePlay(loc, reason) {
  if (reason.indexOf("留资成本") >= 0 || reason.indexOf("开口成本") >= 0)
    return "成本管控：复核出价与定向，暂停高成本计划；将预算向低成本版位/计划转移，优先止损。";
  if (reason.indexOf("消耗下降") >= 0)
    return "消耗恢复：排查是否主动降预算；对低效计划重启并小步扩量，避免份额流失。";
  if (reason.indexOf("CTR") >= 0 || reason.indexOf("曝光") >= 0 || reason.indexOf("点击") >= 0)
    return "点击优化：更换笔记封面/标题，降低素材疲劳，提升点击率。";
  if (reason.indexOf("开口率") >= 0)
    return "开口优化：优化私信承接话术与落地页，提升开口率。";
  if (reason.indexOf("留资率") >= 0 || reason.indexOf("留资转化") >= 0)
    return "留资优化：简化留资表单、强化利益点引导，提升留资转化。";
  return "综合优化：结合下方同赛道历史打法，针对性调整投放结构。";
}

function buildWorkbenchData() {
  const cust = (SNAP.customers || []);
  const reps = (SNAP.reports || {});
  const cases = (SNAP.cases || []);
  const rows = cust.map(c => {
    const rep = reps[String(c.id)];
    return { c, rep, h: computeHealth(rep, c.name) };
  });
  return { rows, cases };
}

function buildPlaybook(c, rep, cases) {
  const ch5 = rep ? (rep.chapters && rep.chapters["5_异常与原因"]) : null;
  const top3 = ch5 ? (ch5.top3_detail || []) : [];
  const plays = [];
  if (!top3.length) {
    plays.push({ loc: "—", reason: "本期无显著异常", action: "维持当前投放，关注下周环比变化。", evidence: [] });
  } else {
    top3.forEach(a => {
      plays.push({
        loc: a.location || "",
        reason: a.reason || "",
        action: rulePlay(a.location || "", a.reason || ""),
        evidence: a.evidence || [],
      });
    });
  }
  const sector = c.sector;
  const matched = cases.filter(x => x.sector === sector && x.referenceable && x.action_taken && x.action_taken.indexOf("待补") < 0).slice(0, 3);
  return { plays, matched };
}

function buildCoverage(rows, cases) {
  const caseSectors = new Set(cases.filter(x => x.referenceable).map(x => x.sector));
  return rows.map(({ c, rep, h }) => {
    const hasReport = !!rep;
    const ch5 = rep ? (rep.chapters && rep.chapters["5_异常与原因"]) : null;
    const hasAnom = !!(ch5 && (ch5.top3_detail || []).length);
    const hasCase = caseSectors.has(c.sector);
    const gap = (!hasReport) || (!hasCase);
    return { c, hasReport, hasAnom, hasCase, gap };
  });
}

/* ---------------- 渲染 ---------------- */
function wbHealthBadge(score, tier) {
  const t = WB_TIER[tier] || WB_TIER["未知"];
  return '<span class="wb-badge" style="color:' + t.color + ';background:' + t.bg + '">' + (score == null ? "—" : score) + " · " + tier + "</span>";
}

function renderWorkbench() {
  const host = document.getElementById("workbenchView");
  if (!host) return;
  const { rows, cases } = buildWorkbenchData();
  if (!rows.length) { host.innerHTML = '<div class="card">暂无可分析客户数据。</div>'; return; }

  // 概览 KPI
  const scored = rows.filter(r => r.h.score != null);
  const avg = scored.length ? Math.round(scored.reduce((s, r) => s + r.h.score, 0) / scored.length) : "—";
  const warnN = rows.filter(r => r.h.tier === "预警").length;
  const watchN = rows.filter(r => r.h.tier === "关注").length;
  const healthyN = rows.filter(r => r.h.tier === "健康").length;
  const sectors = new Set(rows.map(r => r.c.sector));
  const coveredSectors = new Set(cases.filter(x => x.referenceable).map(x => x.sector));
  const coverN = [...sectors].filter(s => coveredSectors.has(s)).length;

  // 预警列表（仅预警档，按分数升序，作为全站唯一重点区）
  const warns = rows.filter(r => r.h.tier === "预警").sort((a, b) => (a.h.score || 999) - (b.h.score || 999));

  // 客户下拉（打法推荐）
  const selOpts = rows.map(r => '<option value="' + r.c.id + '">' + esc(r.c.name) + "（" + esc(r.c.sector) + "）</option>").join("");

  let h = "";
  h += '<div class="wb-head"><h2>客户经营</h2><span class="wb-sub">销售工作台 · 客户经营看板（只读分析）</span></div>';

  // KPI 卡
  h += '<div class="wb-kpis">';
  h += wbKpi("客户总数", rows.length);
  h += wbKpi("平均健康分", avg);
  h += wbKpi("预警客户", warnN, WB_TIER["预警"].color);
  h += wbKpi("关注客户", watchN, WB_TIER["关注"].color);
  h += wbKpi("案例覆盖赛道", coverN + " / " + sectors.size);
  h += "</div>";

  // 分群分布
  h += '<div class="wb-grid2">';
  h += '<div class="card"><h3 class="wb-h3">客户分群分布</h3>' + wbTierBar(healthyN, watchN, warnN, rows.length) + "</div>";
  h += '<div class="card"><h3 class="wb-h3">评分逻辑（可解释）</h3>' +
       '<p class="wb-note">满分 100：① 消耗动能（近 7 天 vs 前 7 天，下滑扣分上限 25）② 指标变化惩罚（上限 35）③ 状态修正（需关注 −10 / 需行动 −22）④ 异常惩罚（上限 10）。≥80 健康，60–79 关注，<60 预警。</p></div>';
  h += "</div>";

  // 预警列表
  h += '<div class="card" style="margin-top:14px;"><h3 class="wb-h3">风险预警（' + warns.length + "）</h3>";
  if (!warns.length) h += '<p class="wb-note">当前无客户触发预警。</p>';
  else {
    h += '<div class="wb-warnlist">';
    warns.forEach(r => {
      h += '<div class="wb-warnitem" onclick="wbSelect(' + r.c.id + ')">' +
           '<div class="wb-wl-left">' + wbHealthBadge(r.h.score, r.h.tier) + "<b>" + esc(r.c.name) + "</b><span class='wb-tag'>" + esc(r.c.sector) + "</span></div>" +
           '<div class="wb-wl-right">' + r.h.warnings.map(w => "<span class='wb-chip'>" + esc(w) + "</span>").join("") + "</div></div>";
    });
    h += "</div>";
  }
  h += "</div>";

  // 客户分群概览（3 卡，不列名单，点开才展开该群客户）
  h += '<div class="card" style="margin-top:14px;"><h3 class="wb-h3">客户分群概览</h3><div class="wb-tiers">' +
       wbTierCard("健康", healthyN, rows) +
       wbTierCard("关注", watchN, rows) +
       wbTierCard("预警", warnN, rows) +
       '</div><div id="wbTierExpand"></div></div>';

  // 打法推荐
  h += '<div class="card" style="margin-top:14px;"><h3 class="wb-h3">打法推荐</h3>' +
       '<div class="wb-selrow"><label>选择客户：</label><select id="wbCustSel" onchange="wbSelect(parseInt(this.value))">' + selOpts + "</select></div>" +
       '<div id="wbPlaybook"></div></div>';

  // 覆盖度自检（按赛道汇总，仅列缺口客户，不堆 51 个名字）
  const cov = buildCoverage(rows, cases);
  const gaps = cov.filter(x => x.gap);
  const secCov = {};
  rows.forEach(r => {
    const s = r.c.sector;
    if (!secCov[s]) secCov[s] = { n: 0, rep: 0, cas: 0 };
    secCov[s].n++;
    if (r.rep) secCov[s].rep++;
    if (coveredSectors.has(s)) secCov[s].cas++;
  });
  h += '<div class="card" style="margin-top:14px;"><h3 class="wb-h3">覆盖度自检（' + gaps.length + " 个缺口）</h3>" +
       '<p class="wb-note">诊断覆盖=已生成报告；案例覆盖=该赛道在案例库有可引用打法。按赛道汇总如下，缺口客户单独列出。</p>' +
       '<div class="table-wrap"><table class="daily"><thead><tr><th>赛道</th><th>客户数</th><th>已诊断</th><th>有案例覆盖</th></tr></thead><tbody>';
  Object.keys(secCov).sort().forEach(s => {
    const d = secCov[s];
    h += "<tr><td>" + esc(s) + "</td><td>" + d.n + "</td><td>" + wbTick(d.n === d.rep) + " " + d.rep + "/" + d.n + "</td><td>" + wbTick(d.cas > 0) + " " + (d.cas > 0 ? "有" : "无") + "</td></tr>";
  });
  h += "</tbody></table></div>";
  if (gaps.length) {
    h += '<div class="wb-gaps"><b>缺口客户：</b>' + gaps.map(x => "<span class='wb-chip wb-chip-bad'>" + esc(x.c.name) + " · " + (x.hasReport ? "缺案例覆盖" : "缺诊断") + "</span>").join(" ") + "</div>";
  }
  h += "</div>";

  host.innerHTML = h;

  // 默认选中第一个预警客户，否则第一个
  const def = warns.length ? warns[0].c.id : rows[0].c.id;
  wbSelect(def);
}

function wbTierCard(tier, n, rows) {
  const t = WB_TIER[tier] || WB_TIER["未知"];
  const inTier = rows.filter(r => r.h.tier === tier);
  const avgT = inTier.length ? Math.round(inTier.reduce((s, r) => s + (r.h.score || 0), 0) / inTier.length) : "—";
  const pct = rows.length ? Math.round(n / rows.length * 100) : 0;
  return '<div class="wb-tier" onclick="wbExpandTier(\'' + tier + '\')">' +
    '<div class="wb-tier-n" style="color:' + t.color + '">' + n + '</div>' +
    '<div class="wb-tier-l" style="color:' + t.color + '">' + tier + '</div>' +
    '<div class="wb-tier-sub">均分 ' + avgT + ' · 占 ' + pct + '%</div>' +
    '<div class="wb-tier-more">点击展开名单 ›</div></div>';
}
function wbExpandTier(tier) {
  const box = document.getElementById("wbTierExpand");
  if (!box) return;
  const { rows } = buildWorkbenchData();
  const inTier = rows.filter(r => r.h.tier === tier).sort((a, b) => (a.h.score == null ? 999 : a.h.score) - (b.h.score == null ? 999 : b.h.score));
  let h = '<div class="wb-expand"><b>' + tier + ' 客户（' + inTier.length + '）</b><div class="wb-explist">';
  inTier.forEach(r => {
    h += '<span class="wb-expri" onclick="wbSelect(' + r.c.id + ')">' + esc(r.c.name) + ' <i>' + (r.h.score == null ? "—" : r.h.score) + '</i></span>';
  });
  h += "</div></div>";
  box.innerHTML = h;
}

function wbKpi(label, val, color) {
  return '<div class="wb-kpi"><div class="wb-kpi-v" style="' + (color ? "color:" + color : "") + '">' + val + '</div><div class="wb-kpi-l">' + label + "</div></div>";
}
function wbTierBar(h, w, warn, total) {
  const p = n => (total ? (n / total * 100) : 0).toFixed(0);
  return '<div class="wb-bar">' +
    '<div class="wb-bar-seg" style="width:' + p(h) + "%;background:" + WB_TIER["健康"].color + '" title="健康 ' + h + '"></div>' +
    '<div class="wb-bar-seg" style="width:' + p(w) + "%;background:" + WB_TIER["关注"].color + '" title="关注 ' + w + '"></div>' +
    '<div class="wb-bar-seg" style="width:' + p(warn) + "%;background:" + WB_TIER["预警"].color + '" title="预警 ' + warn + '"></div>' +
    '</div><div class="wb-legend">' +
    '<span><i style="background:' + WB_TIER["健康"].color + '"></i>健康 ' + h + "</span>" +
    '<span><i style="background:' + WB_TIER["关注"].color + '"></i>关注 ' + w + "</span>" +
    '<span><i style="background:' + WB_TIER["预警"].color + '"></i>预警 ' + warn + "</span></div>";
}
function wbTick(b) { return b ? '<span class="wb-ok">✓</span>' : '<span class="wb-no">—</span>'; }

function wbSelect(id) {
  const sel = document.getElementById("wbCustSel");
  if (sel) sel.value = String(id);
  const { rows, cases } = buildWorkbenchData();
  const row = rows.find(r => r.c.id === id);
  if (!row) return;
  const { plays, matched } = buildPlaybook(row.c, row.rep, cases);
  let h = '<div class="wb-pb-head">' + wbHealthBadge(row.h.score, row.h.tier) + "<b>" + esc(row.c.name) + "</b><span class='wb-tag'>" + esc(row.c.sector) + "</span></div>";
  h += '<div class="wb-plays">';
  plays.forEach(p => {
    h += '<div class="wb-play"><div class="wb-play-loc">' + esc(p.loc) + "</div>" +
         '<div class="wb-play-reason">' + esc(p.reason || "—") + "</div>" +
         '<div class="wb-play-act"><b>建议打法：</b>' + esc(p.action) + "</div>" +
         (p.evidence && p.evidence.length ? '<div class="wb-play-ev">' + p.evidence.map(e => "· " + esc(e)).join("<br>") + "</div>" : "") +
         "</div>";
  });
  h += "</div>";
  if (matched.length) {
    h += '<div class="wb-matched"><h4>同赛道历史打法（案例库）</h4>';
    matched.forEach(m => {
      h += '<div class="wb-match"><span class="wb-match-id">案例 #' + m.id + "</span>" +
           (m.anomaly_signature ? '<span class="wb-match-sig">' + esc(cleanAnomalySig(m.anomaly_signature)) + "</span>" : "") +
           '<div class="wb-match-act">' + esc(m.action_taken || "") + "</div>" +
           (m.result_after ? '<div class="wb-match-res">结果：' + esc(m.result_after) + "</div>" : "") + "</div>";
    });
    h += "</div>";
  } else {
    h += '<div class="wb-matched"><p class="wb-note">该赛道案例库暂无可引用打法，建议从本期异常中沉淀。</p></div>';
  }
  const box = document.getElementById("wbPlaybook");
  if (box) box.innerHTML = h;
}

async function ensureSnap() {
  if (SNAP) return SNAP;
  try { SNAP = await fetch("./snapshot.json").then(r => r.json()); } catch (e) {}
  return SNAP;
}
