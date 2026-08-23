/* 诊断台 · 前端（按 demo 周度复盘demo_卡片到详情.html 移植，数据全部来自后端 API） */

let CUSTOMERS = [];            // [{id,name,ind,sector,cats,st,spend,delta,imp,click,open,lead,cpl,cplPrev,series}]
let DAILY = {};                // {name: [{date,spend,imp,nc,bc,open,lead,ctr,br,lcvr,cpc,cpl}]}
let META = {maxd: '', window: ''};
let BASE_DATA = null;
let WIN = {start:'', end:'', label:'', short:''};
let curIdx = 0;
const OV_FILTER = {ind:'', sec:'', cat:'', cust:'', st:''};   // 当日监控筛选器当前条件
let ATTR_TOKEN = 0;            // 归因请求代际号：重渲染后旧请求不再回填
let OFFLINE = false;           // 无后端 API 时为 true，前端改用 snapshot.json
let SNAP = null;               // 静态快照数据（web/snapshot.json）

const STCLASS = {"需行动":"b-action", "观察":"b-watch", "正常":"b-normal"};
const STCOLOR = {"需行动":"#F97066", "观察":"#FDB022", "正常":"#32D583"};
const METRIC_CN = {CPM:'CPM',CTR:'CTR',CPC:'CPC',button_rate:'按钮率',open_rate:'开口率',
                   lead_rate:'留资率',lead_cvr:'留资转化率',open_cost:'开口成本',lead_cost:'留资成本'};
const LAYER_CN = {placement:'版位',plan:'计划',note:'笔记',funnel:'漏斗'};

/* ---------------- 工具 ---------------- */
function esc(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmt(n){ return (n||0).toLocaleString('en-US'); }
function todayStr(){
  const t=new Date();
  return t.getFullYear()+'-'+String(t.getMonth()+1).padStart(2,'0')+'-'+String(t.getDate()).padStart(2,'0');
}
function shiftDate(s,d){
  const p=s.split('-').map(Number);
  const t=new Date(Date.UTC(p[0],p[1]-1,p[2]));
  t.setUTCDate(t.getUTCDate()+d);
  const y=t.getUTCFullYear(),m=String(t.getUTCMonth()+1).padStart(2,'0'),dd=String(t.getUTCDate()).padStart(2,'0');
  return y+'-'+m+'-'+dd;
}
function computeWin(){
  const base=document.getElementById('fDate').value||META.maxd;
  const range=document.getElementById('fRange').value;
  let start, end=base;
  if(range==='今日'){ start=base; }
  else if(range==='近7天'){ start=shiftDate(base,-6); }
  else if(range==='近14天'){ start=shiftDate(base,-13); }
  else { start=base.slice(0,8)+'01'; }
  WIN={start, end, label:range+' ('+start.slice(5)+' – '+end.slice(5)+')', short:range};
}
function prevWindow(){
  const len=(Math.round((new Date(WIN.end+'T00:00:00Z')-new Date(WIN.start+'T00:00:00Z'))/86400000))+1;
  return {start:shiftDate(WIN.start,-len), end:shiftDate(WIN.start,-1)};
}
function winAgg(all,s,e){
  const t={spend:0,imp:0,nc:0,bc:0,open:0,lead:0,n:0};
  for(const r of all){ if(r.date>=s&&r.date<=e){ t.spend+=r.spend;t.imp+=r.imp;t.nc+=r.nc;t.bc+=r.bc;t.open+=r.open;t.lead+=r.lead;t.n++; } }
  return t;
}

/* ---------------- 窗口重算（卡片指标 + 状态派生） ---------------- */
function recompute(){
  CUSTOMERS.forEach(c=>{
    const all=DAILY[c.name]||[];
    const cur=winAgg(all,WIN.start,WIN.end);
    const n=cur.n||1;
    c.spend=Math.round(cur.spend/n);
    c.imp=Math.round(cur.imp/n);
    c.click=Math.round(cur.nc/n);
    c.open=Math.round(cur.open/n);
    c.lead=Math.round(cur.lead/n);
    c.series=all.filter(r=>r.date>=WIN.start&&r.date<=WIN.end).map(r=>r.spend);
    c.cpl=cur.lead?cur.spend/cur.lead:0;
    const pw=prevWindow();
    const prev=winAgg(all,pw.start,pw.end);
    c.cplPrev=prev.lead?prev.spend/prev.lead:0;
    if(cur.n>0 && prev.n===cur.n){
      const prevAvg=prev.spend/prev.n;
      c.delta=prevAvg?+(((cur.spend/n-prevAvg)/prevAvg)*100).toFixed(1):0;
    } else { c.delta=0; }
    const cplRise=c.cplPrev?+(((c.cpl-c.cplPrev)/c.cplPrev)*100).toFixed(1):0;
    c.cplRise=cplRise;
    if(cplRise>=30 || c.delta<=-25) c.st='需行动';
    else if(cplRise>=12 || c.delta<=-10) c.st='观察';
    else c.st='正常';
  });
}
function renderActive(){
  const a=['overview','list','detail'].find(id=>document.getElementById(id+'View').classList.contains('on'));
  if(a==='overview') renderOverview();
  else if(a==='detail') openDetail(curIdx);
  else applyFilter();
}
async function fetchBase(){
  if(OFFLINE) return;   // 静态快照已在 loadSnapshot 中写入 BASE_DATA
  const pw=prevWindow();
  const qs='?start='+WIN.start+'&end='+WIN.end+'&prev_start='+pw.start+'&prev_end='+pw.end;
  try{
    BASE_DATA=await fetch('/api/base'+qs).then(r=>r.json());
  }catch(e){ BASE_DATA=null; }
}
async function onTimeChange(){
  computeWin(); recompute(); META.window=WIN.label;
  await fetchBase();
  renderActive();
}

/* ---------------- 卡片网格 ---------------- */
function spark(series,color){
  if(!series||series.length<2) return '';
  const w=120,h=28,max=Math.max(...series),min=Math.min(...series),rng=(max-min)||1;
  const pts=series.map((v,i)=>{const x=i/(series.length-1)*w;const y=h-((v-min)/rng)*(h-6)-3;return x.toFixed(1)+','+y.toFixed(1);}).join(' ');
  return '<svg class="mini-chart" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}
function cardHTML(c){
  const up=c.delta>=0;
  return '<div class="card cust-card" onclick="openDetailById('+c.id+')">'
    +'<div class="top"><span class="name">'+esc(c.name)+'</span><span class="badge '+STCLASS[c.st]+'">'+c.st+'</span></div>'
    +'<div class="ind">'+esc(c.ind)+'</div>'
    +'<div class="lab">'+WIN.short+' 日均消耗</div>'
    +'<div class="spend">¥'+fmt(c.spend)+'</div>'
    +'<div class="delta '+(up?'up':'down')+'">'+(up?'▲':'▼')+' 环比 '+(up?'+':'')+c.delta+'%</div>'
    +spark(c.series,STCOLOR[c.st])
    +'</div>';
}
function renderGrid(list){
  document.getElementById('grid').innerHTML = list.length? list.map(c=>cardHTML(c)).join('') : '<div style="color:var(--sub)">无匹配客户</div>';
}
function applyFilter(){
  const ind=document.getElementById('fInd').value, sector=document.getElementById('fSector').value,
        cat=document.getElementById('fCat').value, st=document.getElementById('fSt').value,
        q=document.getElementById('fQ').value.trim();
  const list=CUSTOMERS.filter(c=>(!ind||c.ind===ind)&&(!sector||c.sector===sector)&&(!cat||(c.cats||[]).includes(cat))&&(!st||c.st===st)&&(!q||c.name.includes(q)));
  renderGrid(list);
}
function showView(v){
  ['overview','list','detail','ingest','workbench'].forEach(id=>{const el=document.getElementById(id+'View'); if(el) el.classList.remove('on');});
  const vw=document.getElementById(v+'View'); if(vw) vw.classList.add('on');
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===v));
  const titles={overview:'当日监控',list:'客户详情',ingest:'手动录入',workbench:'客户经营'};
  document.getElementById('pageTitle').innerText=titles[v]||'';
  document.getElementById('topRight').style.display=(v==='ingest'||v==='workbench')?'none':'flex';
  if(v==='overview') renderOverview();
  else if(v==='workbench'){ ensureSnap().then(()=>renderWorkbench()); }
}

/* ---------------- 详情：折线图 + 日表 ---------------- */
function fmtAxis(v, unit){
  if(unit==='%') return v.toFixed(1)+'%';
  if(unit==='元') return '¥'+(Math.round(v*10)/10);
  return Math.round(v).toLocaleString('en-US');
}
function xTicks(n, maxTicks){
  const step=Math.max(1, Math.round(n/maxTicks));
  const idx=[];
  for(let i=0;i<n;i+=step) idx.push(i);
  if(idx[idx.length-1]!==n-1) idx.push(n-1);
  return idx;
}
function lineSVG(values, dates, o){
  o=o||{};
  const w=o.w||660, h=o.h||210, color=o.color||'#3B9DFF', unit=o.unit||'cnt';
  const PL=40, PR=12, PT=12, PB=20;
  const pw=w-PL-PR, ph=h-PT-PB;
  if(!values.length) return '<svg viewBox="0 0 '+w+' '+h+'" width="100%" height="'+h+'"></svg>';
  const max=Math.max(...values), min=Math.min(...values), rng=(max-min)||1;
  const n=values.length;
  const X=i=> PL + (n===1?pw/2:i*pw/(n-1));
  const Y=v=> PT + ph - (v-min)/rng*ph;
  const pts=values.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
  const area='M'+X(0).toFixed(1)+','+(PT+ph).toFixed(1)+' L'+pts.split(' ').join(' L ')+' L'+X(n-1).toFixed(1)+','+(PT+ph).toFixed(1)+' Z';
  const dots=values.map((v,i)=>'<circle cx="'+X(i).toFixed(1)+'" cy="'+Y(v).toFixed(1)+'" r="2.1" fill="'+color+'"/>').join('');
  const mid=(max+min)/2;
  const yaxis='<line x1="'+PL+'" y1="'+PT+'" x2="'+PL+'" y2="'+(PT+ph).toFixed(1)+'" stroke="#E9EDF5" stroke-width="1"/>'
    +'<line x1="'+PL+'" y1="'+Y(mid).toFixed(1)+'" x2="'+(PL+pw).toFixed(1)+'" y2="'+Y(mid).toFixed(1)+'" stroke="#F1F4F9" stroke-width="1" stroke-dasharray="3 3"/>'
    +'<text x="'+(PL-5)+'" y="'+(PT+3)+'" text-anchor="end" font-size="9" fill="#94A3B8">'+fmtAxis(max,unit)+'</text>'
    +'<text x="'+(PL-5)+'" y="'+(PT+ph).toFixed(1)+'" text-anchor="end" font-size="9" fill="#94A3B8">'+fmtAxis(min,unit)+'</text>';
  const ticks=xTicks(n, o.maxTicks||7);
  let xaxis='<line x1="'+PL+'" y1="'+(PT+ph).toFixed(1)+'" x2="'+(PL+pw).toFixed(1)+'" y2="'+(PT+ph).toFixed(1)+'" stroke="#E9EDF5" stroke-width="1"/>';
  ticks.forEach(i=>{ xaxis+='<text x="'+X(i).toFixed(1)+'" y="'+(PT+ph+13).toFixed(1)+'" text-anchor="middle" font-size="9" fill="#94A3B8">'+esc((dates[i]||'').slice(5))+'</text>'; });
  const last=values[n-1];
  const lastLabel='<text x="'+(PL+pw).toFixed(1)+'" y="'+(Y(last)-6).toFixed(1)+'" text-anchor="end" font-size="10" font-weight="800" fill="'+color+'">'+fmtAxis(last,unit)+'</text>';
  return '<svg viewBox="0 0 '+w+' '+h+'" width="100%" height="'+h+'">'
    +'<defs><linearGradient id="g'+color.replace('#','')+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+color+'" stop-opacity=".18"/><stop offset="100%" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>'
    +'<path d="'+area+'" fill="url(#g'+color.replace('#','')+')"/>'
    +yaxis+xaxis
    +'<polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"'+(o.dash?' stroke-dasharray="5 4"':'')+'/>'
    +dots+lastLabel+'</svg>';
}
function winDays(name){
  return (DAILY[name]||[]).filter(d=>d.date>=WIN.start && d.date<=WIN.end).slice();
}
function renderDetailCharts(){
  const c=CUSTOMERS[curIdx];
  const ds=winDays(c.name), dates=ds.map(d=>d.date);
  const keys=['spend','ctr','lcvr','cpl'];
  const colors={spend:'#3B9DFF',ctr:'#32D583',lcvr:'#FDB022',cpl:'#F97066'};
  const labels={spend:'消耗(元)',ctr:'点击率(%)',lcvr:'留资CVR(%)',cpl:'留资成本(元)'};
  const units={spend:'元',ctr:'%',lcvr:'%',cpl:'元'};
  document.getElementById('chartSecTitle').innerText=WIN.short+'趋势 · 多指标分面 ('+WIN.start.slice(5)+' – '+WIN.end.slice(5)+')';
  if(!ds.length){ document.getElementById('detailCharts').innerHTML='<div style="color:var(--sub);font-size:12px;padding:20px;text-align:center;">该区间无数据</div>'; return; }
  document.getElementById('detailCharts').innerHTML='<div class="small-grid">'+keys.map(k=>{
    const vals=ds.map(d=>d[k]);
    const last=vals[vals.length-1];
    return '<div class="small-cell"><div class="t"><span>'+labels[k]+'</span><b>'+fmtAxis(last,units[k])+'</b></div>'+lineSVG(vals,dates,{w:300,h:150,color:colors[k],unit:units[k],maxTicks:3})+'</div>';
  }).join('')+'</div>';
}
function renderDetailTable(){
  const c=CUSTOMERS[curIdx];
  const ds=winDays(c.name);
  const cols=[['date','日期',null],['spend','消耗',2],['imp','曝光',0],['nc','笔记点击',0],['bc','按钮点击',0],['open','私信开口',0],['lead','留资',0],['ctr','点击率%',2],['br','按钮率%',2],['lcvr','留资CVR%',2],['cpc','CPC',2],['cpl','留资成本',2]];
  document.getElementById('tableSecTitle').innerText=WIN.short+'每日数据 ('+WIN.start.slice(5)+' – '+WIN.end.slice(5)+')';
  if(!ds.length){ document.getElementById('detailTable').innerHTML='<tbody><tr><td style="color:var(--sub);padding:20px;">该区间无数据</td></tr></tbody>'; return; }
  let h='<thead><tr>'+cols.map(c=>'<th'+(c[2]===null?'':' class="num"')+'>'+c[1]+'</th>').join('')+'</tr></thead><tbody>';
  ds.forEach(d=>{
    h+='<tr>'+cols.map(c=>{
      const v=d[c[0]];
      const txt=(c[2]==null)? v : (v==null?'':Number(v).toLocaleString('en-US',{minimumFractionDigits:c[2],maximumFractionDigits:c[2]}));
      return '<td'+(c[2]==null?'':' class="num"')+'>'+txt+'</td>';
    }).join('')+'</tr>';
  });
  h+='</tbody>';
  document.getElementById('detailTable').innerHTML=h;
}
function openDetailById(id){
  const i=CUSTOMERS.findIndex(c=>c.id==id);
  if(i>=0) openDetail(i);
}
function openDetail(i){
  curIdx=i; const c=CUSTOMERS[i];
  document.getElementById('dName').innerText=c.name+' · '+c.ind+' · '+WIN.label+' 详细数据';
  document.getElementById('dMeta').innerText='周期 '+META.window+' · 状态：'+c.st;
  document.getElementById('dSpend').innerText='¥'+fmt(c.spend);
  document.getElementById('dDelta').innerText=''+WIN.short+'日均消耗 · 环比 '+(c.delta>=0?'+':'')+c.delta+'%';
  document.getElementById('dKpi').innerHTML=
    '<div class="card kpi"><div class="lab">曝光(日均)</div><div class="v">'+fmt(c.imp)+'</div></div>'
    +'<div class="card kpi"><div class="lab">点击(日均)</div><div class="v">'+fmt(c.click)+'</div></div>'
    +'<div class="card kpi"><div class="lab">开口(日均)</div><div class="v">'+fmt(c.open)+'</div></div>'
    +'<div class="card kpi"><div class="lab">留资(日均)</div><div class="v">'+fmt(c.lead)+'</div></div>';
  document.getElementById('shareLink').classList.remove('on');
  renderDetailCharts();
  renderDetailTable();
  showView('detail');
}

/* ---------------- 重点变化归因（后端确定性归因，无 LLM） ---------------- */
function attribFallback(c){
  return '日均消耗 ¥'+fmt(c.spend)+'（环比 '+(c.delta>=0?'+':'')+c.delta+'%）· 留资 '+c.lead+'/天 · 留资成本 ¥'+Math.round(c.cpl)+'（环比 '+(c.cplRise>=0?'+':'')+c.cplRise+'%）';
}
async function loadAttrib(list, token){
  const ids=[...new Set(list.map(c=>c.id).filter(x=>x))];
  if(!ids.length) return;
  if(OFFLINE){
    if(token!==ATTR_TOKEN) return;
    for(const c of list){
      const el=document.getElementById('attr-'+c.id);
      if(!el) continue;
      const t=SNAP&&SNAP.attrib?SNAP.attrib[String(c.id)]:null;
      el.textContent=t||attribFallback(c);
    }
    return;
  }
  try{
    const pw=prevWindow();
    const res=await fetch('/api/attrib',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids:ids, cur_start:WIN.start, cur_end:WIN.end, cmp_start:pw.start, cmp_end:pw.end})
    }).then(r=>r.json());
    if(token!==ATTR_TOKEN || !res || !res.reasons) return;
    for(const c of list){
      const el=document.getElementById('attr-'+c.id);
      if(!el) continue;
      const t=res.reasons[String(c.id)];
      if(t) el.textContent=t;
      else if(res.error) el.textContent='归因失败：'+res.error;
    }
  }catch(e){
    console.error('归因请求失败:', e);
    for(const c of list){
      const el=document.getElementById('attr-'+c.id);
      if(el && el.textContent==='归因计算中…') el.textContent=attribFallback(c);
    }
  }
}

/* ---------------- 报告生成（真实 LLM） ---------------- */
let shareUrl='';
let curReportId=null;
let reviewLocked=false;
async function generateReport(){
  const c=CUSTOMERS[curIdx];
  if(OFFLINE){
    const rep=SNAP&&SNAP.reports?SNAP.reports[String(c.id)]:null;
    document.getElementById('mTitle').innerText=c.name+' · 复盘报告（静态快照）';
    document.getElementById('reportModal').classList.add('on');
    document.getElementById('shareLink').classList.remove('on');
    document.getElementById('reviewBar').classList.remove('on');
    document.getElementById('rvResult').innerHTML='<span class="muted">静态快照 · 只读，生成 / 审核请在本地后端操作</span>';
    if(rep){ renderReportModal(rep, c); }
    else { document.getElementById('mBody').innerHTML='<div class="loading">该客户暂未在线上生成报告（可在本地后端生成后重新导出快照）。</div>'; }
    return;
  }
  const btn=document.getElementById('genBtn');
  document.getElementById('mTitle').innerText=c.name+' · '+META.window+' 复盘报告';
  document.getElementById('mBody').innerHTML='<div class="loading">报告生成中：LLM 基于当前窗口真实数据撰写八章节，约需 30~90 秒，请勿关闭弹层…</div>';
  document.getElementById('reportModal').classList.add('on');
  document.getElementById('shareLink').classList.remove('on');
  document.getElementById('reviewBar').classList.remove('on');
  document.getElementById('rvResult').innerHTML='';
  btn.disabled=true; btn.innerText='生成中…';
  try{
    const pw=prevWindow();
    const res=await fetch('/api/report',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({customer_id:c.id, cur_start:WIN.start, cur_end:WIN.end, cmp_start:pw.start, cmp_end:pw.end})
    }).then(r=>r.json());
    if(res.error){
      document.getElementById('mBody').innerHTML='<div class="loading" style="color:var(--red);">生成失败：'+esc(res.error)+'</div>';
      return;
    }
    renderReportModal(res.report, c);
    shareUrl=res.share_url;
    document.getElementById('sharePath').innerText=location.origin+shareUrl;
    document.getElementById('shareLink').classList.add('on');
    curReportId=res.report_id||res.report.report_id||null;
    reviewLocked=false;
    if(curReportId){
      document.getElementById('reviewBar').classList.add('on');
      if(res.customer_has_case){
        setReviewAlready(res.case_count);
      }else{
        document.getElementById('rvResult').innerHTML='<span class="muted">案例库当前 '+esc(res.case_count!=null?res.case_count:'-')+' 条 · 审核通过后 +1</span>';
      }
    }
  }catch(e){
    document.getElementById('mBody').innerHTML='<div class="loading" style="color:var(--red);">生成失败：'+esc(e.message||e)+'</div>';
  }finally{
    btn.disabled=false; btn.innerText='生成客户复盘报告';
  }
}
function setReviewAlready(caseCount){
  reviewLocked=true;
  const okBtn=document.getElementById('rvOkBtn');
  okBtn.disabled=true; okBtn.innerText='✓ 已入案例库';
  document.getElementById('rvResult').innerHTML='<span style="color:var(--green);font-weight:700;">该客户已入案例库，无需重复审核</span>'
    +(caseCount!=null?' <span class="muted">· 案例库现有 '+esc(caseCount)+' 条</span>':'');
}
async function submitReview(action){
  if(!curReportId){ alert('报告尚未生成或无 report_id'); return; }
  const result=document.getElementById('rvResult');
  const okBtn=document.getElementById('rvOkBtn'), noBtn=document.getElementById('rvNoBtn');
  const reason=document.getElementById('rejectReason').value.trim();
  if(action==='reject'&&!reason){ result.innerHTML='<span style="color:var(--red);">驳回必须填写理由（将记入 badcase 库）</span>'; return; }
  okBtn.disabled=true; noBtn.disabled=true;
  okBtn.innerText='提交中…';
  try{
    const res=await fetch('/api/review',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({report_id:curReportId, action:action, reason:reason})
    }).then(r=>r.json());
    if(res.error){ result.innerHTML='<span style="color:var(--red);">审核失败：'+esc(res.error)+'</span>'; return; }
    if(action==='approve'){
      const p=res.promote||{};
      let t='已通过并沉淀进案例库';
      if(p.duplicated) t='已通过（该报告/客户此前已入过案例库，未重复入库）';
      else t+='（签名：'+esc(p.signature||'-')+'）';
      if(res.case_count!=null) t+=' · 案例库现有 '+esc(res.case_count)+' 条';
      result.innerHTML='<span style="color:var(--green);font-weight:700;">'+t+'</span>';
      setReviewAlready(res.case_count);
    }else{
      const b=res.badcase||{};
      let t='已驳回并记入 badcase 库';
      if(b.duplicated) t='已驳回（badcase 已存在，未重复记录）';
      result.innerHTML='<span style="color:var(--red);font-weight:700;">'+t+'</span> <span class="muted">修复后可在库中把 status 改为 fixed</span>';
    }
  }catch(e){
    result.innerHTML='<span style="color:var(--red);">审核请求失败：'+esc(e.message||e)+'</span>';
  }finally{
    if(!reviewLocked){
      okBtn.disabled=false; noBtn.disabled=false;
      okBtn.innerText='✓ 通过，入案例库';
    }
  }
}
/* ---------------- 报告弹层：趋势折线图（与当日监控分面小图同款视觉） ---------------- */
function reportTrendChart(t, mName){
  const daily=((t&&t.daily)||[]).filter(x=>x.value!=null);
  if(daily.length<2) return '<p class="muted">趋势数据不足</p>';
  const W=340,H=150,PL=42,PR=10,PT=12,PB=26;
  const w=W-PL-PR, h=H-PT-PB;
  const vals=daily.map(x=>x.value);
  const mx=Math.max(...vals), mn=Math.min(...vals), rng=(mx-mn)||1;
  const X=i=>PL+i*w/(vals.length-1);
  const Y=v=>PT+h-((v-mn)/rng)*h;
  const path=vals.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join('');
  const avg=vals.reduce((a,b)=>a+b,0)/vals.length;
  const fv=v=>Math.abs(v)>=100?Math.round(v).toLocaleString('en-US'):v.toFixed(2);
  const step=Math.max(1,Math.ceil(vals.length/5));
  const ticks=[];
  for(let i=0;i<vals.length;i+=step) ticks.push(i);
  if(ticks[ticks.length-1]!==vals.length-1) ticks.push(vals.length-1);
  const xT=ticks.map(i=>{
    const x=X(i), y=PT+h+12;
    return '<text class="ov-t-axis" x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" text-anchor="end" transform="rotate(-35,'+x.toFixed(1)+','+y.toFixed(1)+')">'+esc((daily[i].date||'').slice(5))+'</text>';
  }).join('');
  return '<div style="margin:10px 0 4px;">'
    +'<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;"><span>近14天'+esc(mName)+'趋势</span><span class="muted">均值 ¥'+fv(avg)+'</span></div>'
    +'<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;">'
      +'<line x1="'+PL+'" y1="'+(PT+h)+'" x2="'+(PL+w)+'" y2="'+(PT+h)+'" stroke="#E9EDF5"/>'
      +'<line x1="'+PL+'" y1="'+Y(avg).toFixed(1)+'" x2="'+(PL+w)+'" y2="'+Y(avg).toFixed(1)+'" stroke="#94A3B8" stroke-dasharray="4 4" stroke-width="1"/>'
      +'<path d="'+path+'" fill="none" stroke="#1E6FD9" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
      +vals.map((v,i)=>'<circle cx="'+X(i).toFixed(1)+'" cy="'+Y(v).toFixed(1)+'" r="2.2" fill="#1E6FD9" opacity="'+(i===vals.length-1?'1':'0.45')+'"/>').join('')
      +'<circle cx="'+X(vals.length-1).toFixed(1)+'" cy="'+Y(vals[vals.length-1]).toFixed(1)+'" r="3.6" fill="#FFF" stroke="#1E6FD9" stroke-width="2.5"/>'
      +'<text class="ov-t-axis" x="'+(PL-4)+'" y="'+(PT+7)+'" text-anchor="end">¥'+fv(mx)+'</text>'
      +'<text class="ov-t-axis" x="'+(PL-4)+'" y="'+(PT+h+3)+'" text-anchor="end">¥'+fv(mn)+'</text>'
      +xT
    +'</svg></div>';
}

function normReportText(s){
  if(typeof s!=='string') return s;
  return s.replace(/open_cost/g,'开口成本').replace(/lead_cost/g,'留资成本')
          .replace(/feed/g,'信息流').replace(/search/g,'搜索');
}
function isPlaceholder(s){
  if(!s) return true;
  s=String(s).trim();
  if(s===''||s==='{}'||s==='[]') return true;
  if(s.indexOf('（待补')>=0||s.indexOf('待 LLM')>=0||s.indexOf('审核时补充')>=0) return true;
  return false;
}
function cleanAnomalySig(sig){
  if(!sig) return '';
  if(typeof sig==='object'){
    const loc=sig.location||'', reason=sig.reason||'';
    return (loc||reason)?(loc+'：'+reason):JSON.stringify(sig);
  }
  let s=String(sig).trim();
  // 形如 "{...}" 的 Python/JSON 字典字符串 -> 提取 location/reason
  if(s.startsWith('{') && s.indexOf(':')>0){
    try{
      const d=JSON.parse(s.replace(/'/g,'"'));
      if(d&&typeof d==='object'){
        const loc=d.location||'', reason=d.reason||'';
        if(loc||reason) return (loc+'：'+reason);
      }
    }catch(e){}
    // 退路：截断/畸形字典串，用正则抽 location / reason
    const ml=s.match(/['"]location['"]\s*:\s*['"]([^'"]*)/);
    const mr=s.match(/['"]reason['"]\s*:\s*['"]([^'"]*)/);
    if(ml||mr){
      const loc=ml?ml[1]:'', reason=mr?mr[1]:'';
      if(loc||reason) return (loc+'：'+reason);
    }
    s=s.replace(/[{}]/g,'').replace(/'/g,'').replace(/"/g,'');
  }
  return s;
}
function clip(s,n){
  s=String(s||'');
  return s.length>n? s.slice(0,n)+'…' : s;
}
function renderReportModal(report, c){
  const ch=(report&&report.chapters)||{};
  const cover=ch['1_封面']||{}, concl=ch['2_核心结论']||{}, metrics=ch['3_指标与趋势']||{},
        layers=ch['4_分层诊断']||[], anomalies=ch['5_异常与原因']||{}, cases=ch['6_案例参考']||{},
        suggests=ch['7_优化建议']||[], actions=ch['8_行动计划']||[];
  const mc=metrics.metrics_cur||{}, mp=metrics.metrics_prev||{};
  let h='';
  h+='<div class="chapter"><h4>① 封面</h4><p>'+esc(cover.customer||c.name)+' · '+esc(cover.industry||c.ind)+' / '+esc(cover.sector||'')+' / '+esc((cover.categories||[]).join('、'))+'</p><p class="muted">周期 '+esc(cover.period||'')+' · 生成于 '+esc(cover.generated_at||'')+'</p></div>';
  const status=report.overall_status||concl.overall_status||'';
  h+='<div class="chapter"><h4>② 核心结论</h4><p><span class="tag">'+esc(status)+'</span>数据状态：'+esc(concl.data_status||'')+'</p><p>'+esc(normReportText(concl.summary||''))+'</p>'
    +((concl.top3||[]).length?'<ul>'+concl.top3.map(x=>'<li>'+esc(normReportText(x.location||''))+'：'+(typeof x.change==='number'?((x.change*100).toFixed(1)+'%'):esc(x.change))+'</li>').join('')+'</ul>':'')+'</div>';
  const RATE_METRICS=new Set(['CTR','button_rate','open_rate','lead_rate','lead_cvr']);
  let mrows='';
  Object.keys(mc).forEach(k=>{
    const cur=mc[k], prev=mp[k];
    const chg=(typeof cur==='number'&&typeof prev==='number'&&prev)?(((cur-prev)/prev*100).toFixed(1)+'%'):'—';
    const f=v=>{ if(typeof v!=='number') return '—';
                 if(RATE_METRICS.has(k)) return (v*100).toFixed(2)+'%';
                 return (Math.abs(v)>=100?Math.round(v).toLocaleString('en-US'):v.toFixed(2)); };
    mrows+='<tr><td>'+esc(METRIC_CN[k]||k)+'</td><td>'+f(cur)+'</td><td>'+f(prev)+'</td><td>'+chg+'</td></tr>';
  });
  const tName=METRIC_CN[metrics.trend_metric]||metrics.trend_metric||'目标成本';
  const chartSpend=reportTrendChart(metrics.trend_spend,'消耗');
  const chartCost=reportTrendChart(metrics.trend_14d,tName);
  h+='<div class="chapter"><h4>③ 指标与趋势</h4>'+(mrows?'<table><thead><tr><th>指标</th><th>本期</th><th>上期</th><th>环比</th></tr></thead><tbody>'+mrows+'</tbody></table>':'<p class="muted">无</p>')+'<div class="report-trend-pair">'+chartSpend+chartCost+'</div></div>';
  h+='<div class="chapter"><h4>④ 分层诊断</h4>'+((layers||[]).length?layers.map(x=>'<div class="item"><span class="tag">'+esc(LAYER_CN[x.layer]||x.layer)+'</span><b>'+esc(x.status)+'</b><p style="margin:4px 0 0;">'+esc(x.judgement||'')+'</p></div>').join(''):'<p class="muted">无</p>')+'</div>';
  const top3detail = Array.isArray(anomalies.top3_detail) ? anomalies.top3_detail : [];
  h+='<div class="chapter"><h4>⑤ 异常与原因</h4>'+(top3detail.length?top3detail.map(x=>'<div class="item"><b>'+x.rank+' '+esc(normReportText(x.location||''))+'</b><p>'+esc(normReportText(x.reason||''))+'</p>'+((x.evidence||[]).length?'<ul>'+x.evidence.map(e=>'<li>'+esc(normReportText(e))+'</li>').join('')+'</ul>':'')+'</div>').join(''):'<p class="muted">无明显异常</p>')+'</div>';
  const caseRefs=(cases.refs&&cases.refs.length)?cases.refs:(cases.cases||[]);
  h+='<div class="chapter"><h4>⑥ 案例参考</h4>'+(caseRefs.length?caseRefs.map(x=>{
    const sig=cleanAnomalySig(x.anomaly_signature);
    const act=isPlaceholder(x.action_taken)?'':normReportText(x.action_taken);
    const sim=isPlaceholder(x.similarity_points)?'':clip(normReportText(x.similarity_points),220);
    return '<div class="item"><b>案例 #'+(x.case_id!=null?x.case_id:'')+'</b>'
      +(x.sector?' <span class="tag">'+esc(x.sector)+'</span>':'')
      +(sig?'<p>异常：'+esc(sig)+'</p>':'')
      +(act?'<p>打法：'+esc(act)+'</p>':'')
      +(sim?'<p class="muted">相似点：'+esc(sim)+'</p>':'')
      +'</div>';
  }).join(''):'<p class="muted">'+esc(cases.note||'暂无可引用案例')+'</p>')+'</div>';
  h+='<div class="chapter"><h4>⑦ 优化建议</h4>'+((suggests||[]).length?suggests.map(x=>'<div class="item">'+(x.priority?'<span class="ptag '+esc(x.priority)+'">'+esc(x.priority)+'</span>':'')+'<p>'+esc(normReportText(x.text||''))+'</p>'+(x.basis?'<p class="muted">依据：'+esc(normReportText(x.basis||''))+'</p>':'')+'</div>').join(''):'<p class="muted">正常周无待办建议</p>')+'</div>';
  h+='<div class="chapter"><h4>⑧ 行动计划</h4>'+((actions||[]).length?'<table><thead><tr><th>行动</th><th>日期</th><th>预期指标</th></tr></thead><tbody>'+actions.map(x=>'<tr><td>'+esc(x.action||'')+'</td><td>'+esc(x.date||'')+'</td><td>'+esc(x.expect_metric||'')+'</td></tr>').join('')+'</tbody></table>':'<p class="muted">无行动计划</p>')
    +'<p class="muted" style="margin-top:8px;">LLM 调用 '+esc(report.llm_calls!=null?report.llm_calls:'-')+' 次 · 成本 ¥'+esc(report.llm_cost_yuan!=null?report.llm_cost_yuan:'-')+'</p></div>';
  document.getElementById('mBody').innerHTML=h;
}
function copyShareLink(){
  if(!shareUrl) return;
  const full=location.origin+shareUrl;
  if(navigator.clipboard) navigator.clipboard.writeText(full);
  else{ const ta=document.createElement('textarea'); ta.value=full; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
}
function openShareLink(){ if(shareUrl) window.open(shareUrl,'_blank'); }
function closeModal(){ document.getElementById('reportModal').classList.remove('on'); }

/* ---------------- 当日监控（5 模块） ---------------- */
function pctDelta(cur, prev){
  if(!prev) return 0;
  return +(((cur-prev)/prev)*100).toFixed(1);
}
function deltaBadge(delta, lowerIsBad){
  if(delta===0) return '<span class="delta flat">—</span>';
  const good = lowerIsBad ? (delta<0) : (delta>0);
  const cls = good?'up':'down';
  const arr = delta>0?'▲':'▼';
  return '<span class="delta '+cls+'"><span class="arr">'+arr+'</span> '+Math.abs(delta).toFixed(1)+'%</span>';
}
function ptDelta(delta){
  if(delta===0) return '<span class="delta flat">—</span>';
  const cls = delta<0?'down':'up';
  const arr = delta>0?'▲':'▼';
  return '<span class="delta '+cls+'"><span class="arr">'+arr+'</span> '+Math.abs(delta).toFixed(1)+'pt</span>';
}
function aggAllInWindow(s,e,list){
  const out={spend:0,imp:0,nc:0,bc:0,open:0,lead:0};
  for(const c of (list||CUSTOMERS)){
    const t=winAgg(DAILY[c.name]||[],s,e);
    out.spend+=t.spend; out.imp+=t.imp; out.nc+=t.nc; out.bc+=t.bc; out.open+=t.open; out.lead+=t.lead;
  }
  return out;
}
function aggDailyTrend(s,e,list){
  const map={};
  for(const c of (list||CUSTOMERS)){
    for(const r of (DAILY[c.name]||[])){
      if(r.date>=s&&r.date<=e){
        if(!map[r.date]) map[r.date]={spend:0,lead:0,open:0};
        map[r.date].spend+=r.spend; map[r.date].lead+=r.lead; map[r.date].open+=r.open;
      }
    }
  }
  const dates=Object.keys(map).sort();
  return dates.map(d=>{const x=map[d];return {date:d,spend:x.spend,cpl:x.lead?x.spend/x.lead:0,copen:x.open?x.spend/x.open:0};});
}
/* 当日监控筛选：读取 5 个下拉的值 → 存入 OV_FILTER → 重渲染 */
function ovApply(){
  const g=id=>{const el=document.getElementById(id);return el?el.value:'';};
  OV_FILTER.ind=g('ovFInd'); OV_FILTER.sec=g('ovFSec'); OV_FILTER.cat=g('ovFCat');
  OV_FILTER.cust=g('ovFCust'); OV_FILTER.st=g('ovFSt');
  renderOverview();
}
function ovCustomers(){
  return CUSTOMERS.filter(c=>
    (!OV_FILTER.ind || c.ind===OV_FILTER.ind)
    && (!OV_FILTER.sec || c.sector===OV_FILTER.sec)
    && (!OV_FILTER.cat || (c.cats||[]).includes(OV_FILTER.cat))
    && (!OV_FILTER.cust || c.name===OV_FILTER.cust)
    && (!OV_FILTER.st || c.st===OV_FILTER.st)
  );
}
function renderOverview(){
  const CS=ovCustomers();   // 筛选后的客户集，本页所有聚合均基于它
  const cur=aggAllInWindow(WIN.start,WIN.end,CS);
  const pw=prevWindow();
  const prev=aggAllInWindow(pw.start,pw.end,CS);
  const CPL=cur.lead?cur.spend/cur.lead:0;
  const CPLprev=prev.lead?prev.spend/prev.lead:0;
  const totalSpendDelta=pctDelta(cur.spend,prev.spend);
  const totalLeadDelta=pctDelta(cur.lead,prev.lead);
  const CPLdelta=pctDelta(CPL,CPLprev);
  const sales=Math.round(cur.spend);

  // 趋势图：分面小图（各自真实 Y 轴 + 均值线 + 平滑曲线）
  const trend=aggDailyTrend(WIN.start,WIN.end,CS);
  const trDates=trend.map(x=>x.date);
  const TREND_SERIES=[
    {name:'消耗',     color:'#3B9DFF', vals:trend.map(x=>x.spend)},
    {name:'留资成本', color:'#22D3EE', vals:trend.map(x=>x.cpl)},
    {name:'开口成本', color:'#FDB022', vals:trend.map(x=>x.copen)},
  ];
  function fmtTrendVal(v){
    if(v>=10000) return (v/10000).toFixed(1)+'万';
    if(v>=100)  return Math.round(v).toLocaleString('en-US');
    return (+v).toFixed(1);
  }
  function linePath(pts){
    if(!pts.length) return '';
    if(pts.length===1) return 'M'+pts[0][0].toFixed(1)+','+pts[0][1].toFixed(1)+' L'+(pts[0][0]+0.01).toFixed(1)+','+pts[0][1].toFixed(1);
    return 'M'+pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' L');
  }
  function miniTrend(s){
    const W=260,H=150,PL=38,PR=10,PT=10,PB=28;
    const w=W-PL-PR, h=H-PT-PB;
    const mx=Math.max(...s.vals), mn=Math.min(...s.vals), rng=(mx-mn)||1;
    const X=i=>PL+(s.vals.length===1?w/2:i*w/(s.vals.length-1));
    const Y=v=>PT+h-((v-mn)/rng)*h;
    const pts=s.vals.map((v,i)=>[X(i),Y(v)]);
    const path=linePath(pts);
    const avg=s.vals.reduce((a,b)=>a+b,0)/s.vals.length;
    const avgY=Y(avg);
    const step=Math.max(1,Math.ceil(s.vals.length/5));
    const ticks=[];
    for(let i=0;i<s.vals.length;i+=step) ticks.push(i);
    if(ticks[ticks.length-1]!==s.vals.length-1) ticks.push(s.vals.length-1);
    const xT=ticks.map(i=>{
      const x=X(i), y=PT+h+12;
      return '<text class="ov-t-axis" x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" text-anchor="end" transform="rotate(-35,'+x.toFixed(1)+','+y.toFixed(1)+')">'+esc((trDates[i]||'').slice(5))+'</text>';
    }).join('');
    return '<div class="ov-trend-mini">'
      +'<div class="head"><div class="name">'+esc(s.name)+'</div><div class="cur" style="color:'+s.color+'">¥'+fmtTrendVal(s.vals[s.vals.length-1])+'</div></div>'
      +'<div class="sub">均值 ¥'+fmtTrendVal(avg)+'</div>'
      +'<svg viewBox="0 0 '+W+' '+H+'">'
        +'<line x1="'+PL+'" y1="'+(PT+h)+'" x2="'+(PL+w)+'" y2="'+(PT+h)+'" stroke="#E9EDF5"/>'
        +'<line class="avg" x1="'+PL+'" y1="'+avgY.toFixed(1)+'" x2="'+(PL+w)+'" y2="'+avgY.toFixed(1)+'"/>'
        +'<path d="'+path+'" fill="none" stroke="'+s.color+'" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
        +pts.map((p,i)=>'<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="2.2" fill="'+s.color+'" opacity="'+(i===s.vals.length-1?'1':'0.45')+'"/>').join('')
        +'<circle cx="'+X(s.vals.length-1).toFixed(1)+'" cy="'+Y(s.vals[s.vals.length-1]).toFixed(1)+'" r="3.6" fill="#FFF" stroke="'+s.color+'" stroke-width="2.5"/>'
        +'<text class="ov-t-axis" x="'+(PL-4)+'" y="'+(PT+7).toFixed(1)+'" text-anchor="end">¥'+fmtTrendVal(mx)+'</text>'
        +'<text class="ov-t-axis" x="'+(PL-4)+'" y="'+(PT+h+3).toFixed(1)+'" text-anchor="end">¥'+fmtTrendVal(mn)+'</text>'
        +xT
      +'</svg>'
      +'</div>';
  }
  const trendHTML= trend.length
    ? '<div class="ov-trend-grid">'+TREND_SERIES.map(miniTrend).join('')+'</div>'
    : '<div class="ov-trend-empty">该区间无数据</div>';

  // 过程数据
  const CTR=cur.imp?cur.nc/cur.imp*100:0, CTRprev=prev.imp?prev.nc/prev.imp*100:0;
  const BR=cur.nc?cur.bc/cur.nc*100:0, BRprev=prev.nc?prev.bc/prev.nc*100:0;
  const LCVR=cur.open?cur.lead/cur.open*100:0, LCVRprev=prev.open?prev.lead/prev.open*100:0;
  const CPC=cur.nc?cur.spend/cur.nc:0, CPCprev=prev.nc?prev.spend/prev.nc:0;

  // 基建情况（来自 /api/base，窗口感知）
  const B=BASE_DATA||{in_notes:0,new_notes:0,in_plans:0,new_plans:0,prev_in_notes:0,prev_new_notes:0,prev_new_plans:0,prev_in_plans:0};
  const inNoteDelta=pctDelta(B.in_notes,B.prev_in_notes);
  const newNoteDelta=pctDelta(B.new_notes,B.prev_new_notes);
  const inPlanDelta=pctDelta(B.in_plans,B.prev_in_plans);
  const newPlanDelta=pctDelta(B.new_plans,B.prev_new_plans);

  // 今日诊断速览：P0=需行动客户，P1=窗口环比下滑最大
  const sorted=[...CS].sort((a,b)=>a.delta-b.delta);
  const drop=sorted.slice(0,3);
  const p0=CS.filter(c=>c.st==='需行动').slice(0,2);
  const alerts=p0.map(c=>({prio:'p0', name:c.name, ind:c.ind, desc:'留资成本 ¥'+Math.round(c.cpl)+'（环比 '+(c.cplRise>=0?'+':'')+c.cplRise+'%），消耗环比 '+(c.delta>=0?'+':'')+c.delta+'%'}));
  drop.forEach(c=>{ if(!alerts.find(a=>a.name===c.name)&&alerts.length<3) alerts.push({prio:'p1', name:c.name, ind:c.ind, desc:'窗口消耗环比 -'+Math.abs(c.delta)+'%，留资 '+c.lead+'/天'}); });
  const p0Count=alerts.filter(a=>a.prio==='p0').length;

  // 客户每日监控表（按 WIN.end 当日消耗排序）
  const lastDate=WIN.end;
  const rows=CS.map(c=>{
    const ds=(DAILY[c.name]||[]);
    const today=ds.find(r=>r.date===lastDate)||{spend:0,open:0,lead:0};
    const prevD=ds.find(r=>r.date===shiftDate(lastDate,-1))||{spend:0};
    const dlt=prevD.spend?+(((today.spend-prevD.spend)/prevD.spend)*100).toFixed(1):0;
    const tasks=c.st==='需行动'?'留资成本超警戒，优先排查'
                :c.st==='观察'?'周环比下滑，持续观察'
                :'—';
    return {name:c.name, ind:c.ind, spend:today.spend, dlt, st:c.st, task:tasks};
  }).sort((a,b)=>b.spend-a.spend);

  // 重点变化归因（排序取 TOP，文案由后端 /api/attrib 确定性归因填充）
  const topDrop=[...CS].sort((a,b)=>a.delta-b.delta).slice(0,5);
  const topRise=[...CS].sort((a,b)=>b.delta-a.delta).slice(0,5);
  function attribHTML(c){
    return '<div class="ov-attrib-item'+(c.delta>=0?' up':'')+'"><div class="t">'+esc(c.name)+' <span style="color:var(--sub);font-weight:700">'+esc(c.ind)+'</span></div><div class="d" id="attr-'+c.id+'">'+esc(attribFallback(c))+'</div></div>';
  }
  ATTR_TOKEN++;
  loadAttrib([...topDrop,...topRise], ATTR_TOKEN);

  const alertsHTML=alerts.length?alerts.map(a=>
    '<div class="ov-alert-item '+a.prio+'"><div class="t"><span class="tag">'+(a.prio==='p0'?'P0':'P1')+'</span>'+esc(a.name)+' · '+esc(a.ind)+'</div><div class="d">'+esc(a.desc)+'</div></div>'
  ).join(''):'<div style="color:var(--sub);font-size:12px;">今日无明显异常</div>';

  const monRowsHTML=rows.slice(0,8).map(r=>
    '<tr><td>'+esc(r.name)+'</td><td>'+esc(r.ind)+'</td><td>¥'+fmt(r.spend)+'</td><td class="'+(r.dlt>=0?'up':'down')+'">'+(r.dlt>=0?'+':'')+r.dlt+'%</td><td><span class="badge '+STCLASS[r.st]+'">'+r.st+'</span></td><td style="font-size:11px;color:var(--ink);">'+esc(r.task)+'</td></tr>'
  ).join('');

  // 筛选器选项（数据驱动；重渲染时保留已选值）
  const ovSel=k=>{const el=document.getElementById(k);return el?el.value:'';};
  const ovKeep={ind:ovSel('ovFInd'), sec:ovSel('ovFSec'), cat:ovSel('ovFCat'), cust:ovSel('ovFCust'), st:ovSel('ovFSt')};
  function ovOpts(list, allLabel, sel){
    return '<option value="">'+allLabel+'</option>'+list.map(v=>'<option'+(v===sel?' selected':'')+'>'+esc(v)+'</option>').join('');
  }
  const ovInds=[...new Set(CUSTOMERS.map(c=>c.ind))].sort();
  const ovSecs=[...new Set(CUSTOMERS.map(c=>c.sector).filter(Boolean))].sort();
  const ovCats=[...new Set(CUSTOMERS.flatMap(c=>c.cats||[]))].sort();
  const ovCusts=CUSTOMERS.map(c=>c.name).sort();
  const ovSts=['需行动','观察','正常'];

  document.getElementById('overviewView').innerHTML=
    /* 模块 1: 顶部 KPI + 筛选器 */
    '<div class="ov-top-kpis">'
    +'<div class="card ov-kpi-c k-spend"><div class="ico">💰</div><div class="body"><div class="lab">总消耗</div><div class="main"><span class="v">¥'+fmt(sales)+'</span>'+deltaBadge(totalSpendDelta,false)+'</div></div></div>'
    +'<div class="card ov-kpi-c k-lead"><div class="ico">🎯</div><div class="body"><div class="lab">留资数</div><div class="main"><span class="v">'+fmt(cur.lead)+'</span>'+deltaBadge(totalLeadDelta,false)+'</div></div></div>'
    +'<div class="card ov-kpi-c k-cpl"><div class="ico">📉</div><div class="body"><div class="lab">留资成本</div><div class="main"><span class="v">¥'+Math.round(CPL)+'</span>'+deltaBadge(CPLdelta,true)+'</div></div></div>'
    +'<div class="card ov-filter-card"><div class="filter-bar">'
      +'<select id="ovFInd">'+ovOpts(ovInds,'全部行业',ovKeep.ind)+'</select>'
      +'<select id="ovFSec">'+ovOpts(ovSecs,'全部赛道',ovKeep.sec)+'</select>'
      +'<select id="ovFCat">'+ovOpts(ovCats,'全部品类',ovKeep.cat)+'</select>'
      +'<select id="ovFCust">'+ovOpts(ovCusts,'全部客户',ovKeep.cust)+'</select>'
      +'<select id="ovFSt">'+ovOpts(ovSts,'全部状态',ovKeep.st)+'</select>'
      +'<button onclick="ovApply()">应用筛选</button>'
    +'</div></div>'
    +'</div>'

    /* 模块 2: 趋势 */
    +'<div class="ov-row2">'
      +'<div class="card ov-chart-card">'
        +'<div class="t">趋势（'+WIN.short+' · 分指标）</div>'
        +trendHTML
      +'</div>'
    +'</div>'

    /* 模块 3: 过程数据 + 基建情况 */
    +'<div class="ov-row2b">'
      +'<div class="card"><div class="t" style="font-size:12px;color:var(--sub);font-weight:800;margin-bottom:12px;">过程数据</div>'
        +'<div class="ov-mini-grid">'
          +'<div class="ov-mini-card"><div class="lab">CTR</div><div class="v">'+CTR.toFixed(1)+'%</div>'+ptDelta(+(CTR-CTRprev).toFixed(2))+'</div>'
          +'<div class="ov-mini-card"><div class="lab">按钮率</div><div class="v">'+BR.toFixed(1)+'%</div>'+ptDelta(+(BR-BRprev).toFixed(2))+'</div>'
          +'<div class="ov-mini-card"><div class="lab">留资CVR</div><div class="v">'+LCVR.toFixed(2)+'%</div>'+ptDelta(+(LCVR-LCVRprev).toFixed(2))+'</div>'
          +'<div class="ov-mini-card"><div class="lab">CPC</div><div class="v">¥'+CPC.toFixed(2)+'</div>'+deltaBadge(pctDelta(CPC,CPCprev),true)+'</div>'
        +'</div>'
      +'</div>'
      +'<div class="card"><div class="t" style="font-size:12px;color:var(--sub);font-weight:800;margin-bottom:12px;">基建情况</div>'
        +'<div class="ov-mini-grid">'
          +'<div class="ov-mini-card"><div class="lab">在投笔记</div><div class="v">'+fmt(B.in_notes)+'</div>'+deltaBadge(inNoteDelta,false)+'</div>'
          +'<div class="ov-mini-card"><div class="lab">新投笔记</div><div class="v">'+fmt(B.new_notes)+'</div>'+deltaBadge(newNoteDelta,false)+'</div>'
          +'<div class="ov-mini-card"><div class="lab">在投计划</div><div class="v">'+fmt(B.in_plans)+'</div>'+deltaBadge(inPlanDelta,false)+'</div>'
          +'<div class="ov-mini-card"><div class="lab">新投计划</div><div class="v">'+fmt(B.new_plans)+'</div>'+deltaBadge(newPlanDelta,false)+'</div>'
        +'</div>'
      +'</div>'
    +'</div>'

    /* 模块 4: 今日诊断速览 + 客户每日监控 */
    +'<div class="ov-row2b">'
      +'<div class="card">'
        +'<div class="ov-alert-head">今日诊断速览 · <b>'+alerts.length+'</b> 个客户需关注</div>'
        +'<div class="ov-alert-sub">其中 <b style="color:var(--red)">'+p0Count+'</b> 项 P0 需当天止损</div>'
        +alertsHTML
      +'</div>'
      +'<div class="card ov-mon-card">'
        +'<div class="t" style="font-size:12px;color:var(--sub);font-weight:800;margin-bottom:12px;">客户每日监控</div>'
        +'<table class="daily"><thead><tr><th>客户</th><th>行业</th><th>今日消耗</th><th>环比</th><th>状态</th><th>今日要务</th></tr></thead><tbody>'
        +monRowsHTML
        +'</tbody></table>'
      +'</div>'
    +'</div>'

    /* 模块 5: 重点变化归因 */
    +'<div class="ov-section-title">重点变化归因</div>'
    +'<div class="ov-row2b">'
      +'<div class="card"><div class="t" style="font-size:12px;color:var(--sub);font-weight:800;margin-bottom:10px;">掉量 TOP</div>'+topDrop.map(attribHTML).join('')+'</div>'
      +'<div class="card"><div class="t" style="font-size:12px;color:var(--sub);font-weight:800;margin-bottom:10px;">增量 TOP</div>'+topRise.map(attribHTML).join('')+'</div>'
    +'</div>';
}

/* ---------------- 手动录入（填表式：上行指标名，下行只填值） ---------------- */
async function doIngestForm(){
  const res=document.getElementById('ingRes');
  const g=id=>document.getElementById(id).value.trim();
  const date=g('iDate'), name=g('iName'), ind=g('iInd')||'到综服务', sec=g('iSec')||ind, cat=g('iCat')||'通用';
  const num=(id)=>{const v=parseFloat(g(id));return isNaN(v)?0:v;};
  const spend=num('iSpend'), imp=num('iImp'), nc=num('iNc'), bc=num('iBc'), open=num('iOpen'), lead=num('iLead');
  if(!date){ res.style.display='block'; res.textContent='请填写日期'; return; }
  if(!name){ res.style.display='block'; res.textContent='请填写客户名'; return; }
  if(!spend&&!imp&&!nc&&!lead){ res.style.display='block'; res.textContent='至少填写一项指标数值'; return; }
  const payload={
    customer:{name:name, industry:ind, sector:sec, categories:[cat], optimize_target:'lead', target_cost:80},
    plans:[{key:0, name:'录入计划_'+cat, category:cat, placement:'feed', created_date:date, status:'在投', daily_budget:Math.max(1,Math.round(spend))}],
    notes:[{key:0, plan_key:0, category:cat, title:'录入笔记_'+cat, material_form:'图文', created_date:date, status:'在投'}],
    daily_metrics:[{plan_key:0, note_key:0, category:cat, placement:'feed', date:date,
      spend:spend, impressions:imp, note_clicks:nc, button_clicks:bc, open_msg:open, lead_cnt:lead}]
  };
  res.style.display='block'; res.textContent='提交中…';
  try{
    const out=await fetch('/api/ingest',{
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    }).then(r=>r.json());
    if(out.error){ res.textContent='录入失败：'+out.error; return; }
    res.textContent='录入成功：'+JSON.stringify(out)+'\n正在生成 LLM 诊断报告…';
    await boot();                                   // 新客户出现在首页/列表
    // 把时间窗口切到录入日，并定位到新客户
    document.getElementById('fDate').value=date;
    document.getElementById('fRange').value='今日';
    await onTimeChange();
    const i=CUSTOMERS.findIndex(c=>c.name===name);
    if(i>=0){ curIdx=i; openDetail(i); }
    await generateReport();                          // 自动 LLM 诊断
    res.textContent='录入成功：'+JSON.stringify(out)+'\n诊断报告已生成（见弹层）。';
    // 清空录入表单，便于下一次录入（日期复位为今天）
    ['iName','iInd','iSec','iCat','iSpend','iImp','iNc','iBc','iOpen','iLead']
      .forEach(id=>{const el=document.getElementById(id); if(el) el.value='';});
    const iDate=document.getElementById('iDate'); if(iDate) iDate.value=todayStr();
  }catch(e){
    res.textContent='请求失败：'+(e.message||e);
  }
}

/* ---------------- 启动 ---------------- */
async function boot(){
  // 开发/调试：?offline=1 强制走静态快照（即使本地有后端）
  if(new URLSearchParams(location.search).get('offline')==='1'){
    await loadSnapshot();
    return;
  }
  try{
    const [custs, daily]=await Promise.all([
      fetch('/api/customers?light=1').then(r=>r.ok?r.json():Promise.reject()),
      fetch('/api/daily').then(r=>r.ok?r.json():Promise.reject())
    ]);
    if(!Array.isArray(custs) || !daily || !daily.customers){
      throw new Error('后端数据格式异常');
    }
    DAILY=daily.customers||{};
    META.maxd=daily.maxd||'';
    document.getElementById('fDate').value=todayStr();   // 默认与真实今天同步
    const iDate=document.getElementById('iDate'); if(iDate&&!iDate.value) iDate.value=todayStr();
    CUSTOMERS=(custs||[]).map(c=>({
      id:c.id, name:c.name, ind:c.industry, sector:c.sector, cats:c.categories,
      st:'正常', spend:0, delta:0, imp:0, click:0, open:0, lead:0, cpl:0, cplPrev:0, cplRise:0, series:[]
    }));
    fillFilterOptions();
    document.getElementById('fQ').addEventListener('input',applyFilter);
    document.getElementById('fDate').addEventListener('change',onTimeChange);
    document.getElementById('fRange').addEventListener('change',onTimeChange);
    await onTimeChange();
  }catch(e){
    console.warn('未检测到后端 API，切换到静态快照模式：', e);
    await loadSnapshot();
  }
}
function fillFilterOptions(){
  const inds=[...new Set(CUSTOMERS.map(c=>c.ind))];
  document.getElementById('fInd').innerHTML='<option value="">全部行业</option>'+inds.map(i=>'<option>'+esc(i)+'</option>').join('');
  const sectors=[...new Set(CUSTOMERS.map(c=>c.sector).filter(Boolean))];
  document.getElementById('fSector').innerHTML='<option value="">全部赛道</option>'+sectors.map(s=>'<option>'+esc(s)+'</option>').join('');
  const cats=[...new Set(CUSTOMERS.flatMap(c=>c.cats||[]))];
  document.getElementById('fCat').innerHTML='<option value="">全部品类</option>'+cats.map(s=>'<option>'+esc(s)+'</option>').join('');
}
/* 无后端时加载静态快照（部署到静态托管后自动走此路） */
async function loadSnapshot(){
  OFFLINE=true;
  const snap=await fetch('./snapshot.json').then(r=>r.json());
  SNAP=snap;
  const customers=snap.customers||[];
  const daily=snap.daily||{};
  DAILY=daily;   // 快照已存内层 {客户名: 日度序列}，无需再取 .customers
  META.maxd=(snap.meta&&snap.meta.maxd)||'';
  CUSTOMERS=customers.map(c=>({
    id:c.id, name:c.name, ind:c.industry, sector:c.sector, cats:c.categories,
    st:'正常', spend:0, delta:0, imp:0, click:0, open:0, lead:0, cpl:0, cplPrev:0, cplRise:0, series:[]
  }));
  document.getElementById('fDate').value=META.maxd||todayStr();
  const iDate=document.getElementById('iDate'); if(iDate) iDate.value=META.maxd||todayStr();
  document.getElementById('fRange').value='近7天';
  BASE_DATA=snap.base||null;
  fillFilterOptions();
  document.getElementById('fQ').addEventListener('input',applyFilter);
  document.getElementById('fDate').addEventListener('change',onTimeChange);
  document.getElementById('fRange').addEventListener('change',onTimeChange);
  showOfflineBanner();
  disableBackendUI();
  await onTimeChange();
}
function showOfflineBanner(){
  let b=document.getElementById('offlineBanner');
  if(!b){
    b=document.createElement('div'); b.id='offlineBanner';
    b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:999;background:#E8F0FE;color:#1D4ED8;'+
      'font-size:12px;text-align:center;padding:6px 10px;border-bottom:1px solid #BACBFA;';
    document.body.prepend(b);
    document.body.style.paddingTop='36px';
  }
  b.textContent='📋 当前为静态只读快照（云端托管，无需后端）。生成新报告 / 审核入库请在本地后端操作。';
}
function disableBackendUI(){
  // 隐藏「手动录入」视图（依赖后端写入）
  const nav=document.querySelector('.nav-item[data-view="ingest"]');
  if(nav) nav.style.display='none';
  const gb=document.getElementById('genBtn');
  if(gb) gb.innerText='查看报告（静态快照）';
}
boot();
