// SEPI dashboard application — extracted from index.html (audit W5). Served with
// ?v=N cache busting; loaded with `defer` so the DOM is parsed before boot() runs.
const $ = id => document.getElementById(id);
const fmt = (v,d=0) => v==null||isNaN(v) ? "–" : Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const usd  = v => v==null ? "–" : (v<0?"-$":"$")+fmt(Math.abs(v),0);
const susd = v => v==null ? "–" : (v>=0?"+$":"-$")+fmt(Math.abs(v),0);
const usd2 = v => v==null ? "–" : (v<0?"-$":"$")+fmt(Math.abs(v),2);
const susd2 = v => v==null ? "–" : (v>=0?"+$":"-$")+fmt(Math.abs(v),2);
const pct  = (v,d=1) => v==null ? "–" : (100*v).toFixed(d)+"%";
const spct = (v,d=1) => v==null ? "–" : (v>=0?"+":"")+(100*v).toFixed(d)+"%";
const sgn  = v => v==null ? "mut" : v>0 ? "pos" : v<0 ? "neg" : "mut";
const SKEL = '<div class="skel-rows"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>';

let META = {env:"paper", leverage_cap:2.0, target_delta:0.30};

// ---- graphical instrument helpers (ticker chip + company name + sector) ----
let REF = {};   // {TICKER: {name, sector, industry}} — instrument labels, loaded once on boot
// Chip colour ENCODES the GICS sector, so the book groups visually by sector at a glance.
const SECTOR_HUE = {
  "Information Technology":210, "Communication Services":280, "Health Care":350,
  "Financials":158, "Consumer Discretionary":22, "Consumer Staples":122, "Energy":42,
  "Industrials":250, "Materials":92, "Utilities":188, "Real Estate":320,
};
function sectorStyle(sec){ const h=SECTOR_HUE[sec];
  return h==null ? "background:hsl(220 8% 26%);color:hsl(220 12% 72%)"      // unknown → neutral grey
                 : `background:hsl(${h} 42% 24%);color:hsl(${h} 72% 78%)`; }
function tkrChip(sym, sec){ return `<span class="tkrchip" data-sym="${sym}" style="${sectorStyle(sec)}">${sym}</span>`; }
function symCell(sym, type){
  sym = String(sym);
  const r = REF[sym.toUpperCase()] || {};
  const nm = r.name || sym;
  const sec = r.sector || "—";
  const mark = type==="call" ? ` <span class="callmark">· covered call</span>` : "";
  const tip = [r.name, r.industry].filter(Boolean).join(" — ") || sym;
  return `<span class="sym-cell" data-tip="${esc(tip)}">${tkrChip(sym, r.sector)}`+
    `<span class="sym-meta"><span class="nm">${nm}</span><span class="sec">${sec}${mark}</span></span></span>`;
}
function allocBar(w, t, max){
  const W=Math.max(0,Math.min(100,(w||0)/max*100)), T=Math.max(0,Math.min(100,(t||0)/max*100));
  return `<div class="alloc" data-tip="Current weight (bar) vs target (tick). Weight = position market value ÷ NAV.">`+
    `<div class="alloc-track"><div class="alloc-fill" style="width:${W.toFixed(0)}%"></div>`+
    `<div class="alloc-tick" style="left:${T.toFixed(0)}%"></div></div><span class="alloc-val">${pct(w)}</span></div>`;
}

// ---- floating tooltips ([data-tip]) + rich ticker hover-card ([data-sym]) ----
const TIP = document.createElement("div"); TIP.id = "tip"; document.body.appendChild(TIP);
const TKP = document.createElement("div"); TKP.id = "tkpop"; TKP.style.display = "none"; document.body.appendChild(TKP);
let tipEl = null, tkEl = null;
function posFloat(el, e, pad){ pad = pad||14; const w = el.offsetWidth, h = el.offsetHeight;
  let x = e.clientX+pad, y = e.clientY+pad;
  if(x+w > innerWidth-8) x = e.clientX-w-pad;
  if(y+h > innerHeight-8) y = e.clientY-h-pad;
  el.style.left = Math.max(8,x)+"px"; el.style.top = Math.max(8,y)+"px"; }
function posTip(e){ posFloat(TIP, e); }

const _quotes = {};
async function loadQuotes(){ const j = await get("/api/quotes"); if(j && j.quotes) Object.assign(_quotes, j.quotes); }
function fmtVol(v){ return v==null ? "–" : v>=1e9?(v/1e9).toFixed(2)+"B" : v>=1e6?(v/1e6).toFixed(2)+"M" : v>=1e3?(v/1e3).toFixed(1)+"k" : fmt(v); }
function tkRange(lo,hi,val){ if(lo==null||hi==null||val==null||hi<=lo) return '<div class="tkp-track"></div>';
  const p = Math.max(0,Math.min(100,(val-lo)/(hi-lo)*100));
  return `<div class="tkp-track"><span class="tkp-fill" style="width:${p}%"></span><span class="tkp-knob" style="left:${p}%"></span></div>`; }
function tkPopHtml(sym){
  sym = String(sym).toUpperCase();
  const q = _quotes[sym]||{}, r = REF[sym]||{}, px = _px[sym];
  const live = (px && px.length) ? px[px.length-1] : q.last;
  const prev = q.prev_close, d = (live!=null && prev) ? (live-prev)/prev : null;
  const c = d==null?"var(--muted)":d>=0?"var(--green)":"var(--red)", arw = d==null?"":d>=0?"▲":"▼";
  const rng = (label,lo,hi) => `<div class="tkp-rng"><div class="tkp-rl">${label}</div>${tkRange(lo,hi,live)}`
    +`<div class="tkp-re"><span>${lo==null?"–":usd2(lo)}</span><span>${hi==null?"–":usd2(hi)}</span></div></div>`;
  const row = (k,v) => `<div class="tkp-row"><span class="tkp-k">${k}</span><span class="tkp-v">${v}</span></div>`;
  const tag = sessionLabel();   // 'Pre-market' / 'After hours' → the % is the extended-hours move vs prior close
  return `<div class="tkp-h"><div><div class="tkp-sym">${sym}</div><div class="tkp-name">${r.name||"—"}</div></div>`
    +`<div class="tkp-rgt"><div class="tkp-px">${live==null?"–":usd2(live)}</div><div class="tkp-chg" style="color:${c}">${arw} ${d==null?"–":spct(d)}</div>${tag?`<div class="tkp-sess">${tag}</div>`:""}</div></div>`
    +rng("Day range", q.low, q.high) + rng("52-week range", q.wk52_low, q.wk52_high)
    +`<div class="tkp-hr"></div>` + row("Open", q.open==null?"–":usd2(q.open))
    + row("Prev close", q.prev_close==null?"–":usd2(q.prev_close)) + row("Volume", fmtVol(q.volume));
}
document.addEventListener("mouseover", e=>{
  const st = e.target.closest("[data-sym]");
  if(st){ tkEl = st; tipEl = null; TIP.style.display = "none";
    TKP.innerHTML = tkPopHtml(st.getAttribute("data-sym")); TKP.style.display = "block"; posFloat(TKP, e, 16); return; }
  tkEl = null; TKP.style.display = "none";
  const t = e.target.closest("[data-tip]"); tipEl = t;
  if(t){ TIP.textContent = t.getAttribute("data-tip"); TIP.style.display = "block"; posTip(e); } else TIP.style.display = "none";
});
document.addEventListener("mousemove", e=>{ if(tkEl) posFloat(TKP, e, 16); else if(tipEl) posTip(e); });
// Touch devices have no hover: tap a ⓘ / any [data-tip] element to toggle its tooltip.
if (matchMedia("(hover: none)").matches) document.addEventListener("click", e=>{
  const t = e.target.closest("[data-tip]");
  if (t && TIP.style.display !== "block"){ TIP.textContent = t.getAttribute("data-tip"); TIP.style.display = "block"; posTip(e); }
  else TIP.style.display = "none";
});

// ---- two-level nav: top tabs + sub-tabs -----------------------------------
const VIEWS = ["overview","portfolio","performance","execute"];
const SUBS  = {portfolio:["holdings","activity"], performance:["returns","risk","attribution","execution"]};
let activeView = "overview";
const activeSub = {portfolio:"holdings", performance:"returns"};

document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  activeView = t.dataset.view;
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  VIEWS.forEach(v => $(v+"-view").style.display = activeView === v ? "block" : "none");
  loadActive();
});

document.querySelectorAll(".subtab").forEach(t => t.onclick = () => {
  const group = t.closest(".subtabs").dataset.group, sub = t.dataset.sub;
  activeSub[group] = sub;
  t.closest(".subtabs").querySelectorAll(".subtab").forEach(x => x.classList.toggle("active", x === t));
  SUBS[group].forEach(s => $("sub-"+s).style.display = sub === s ? "" : "none");
  loadActive();
});

async function loadGlobal(){ renderHealth(await get("/api/health")); }   // global status bar — every tab
function loadActive(){
  loadGlobal();
  if (activeView === "overview") loadOverview();
  else if (activeView === "portfolio") loadPortfolio();
  else if (activeView === "performance") loadPerformance();
  else if (activeView === "execute") loadExecute();
}

async function get(p){ try { const r = await fetch(p); return r.ok ? await r.json() : null; } catch { return null; } }

// ---- Track record (live paper performance vs benchmarks + slippage) -------
const cssv = (n,f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;
// Benchmarks are the engine's set (SPY null hypothesis, BXMD mechanical overwrite, JEPI the
// peer fund, USMV the passive min-vol sleeve check). Teal stays the strategy series;
// green/red are signed-P&L only (§8).
const BENCH_VAR = {SPY:"--spy", BXMD:"--purple", JEPI:"--amber", USMV:"--sec-health"};
function bcol(sym,i){ const palette=["--spy","--purple","--amber","--sec-health"]; return cssv(BENCH_VAR[sym]||palette[i%palette.length], "#8893a3"); }

// ---- Performance tab (built to the design mock: Returns / Risk / Execution) ----------------
// Cache of the last payloads so the in-panel period tabs re-render without a refetch.
let _perfTR=null, _perfSlip=null, _perfFees=null, _perfRisk=null, _perfSt=null, _perfRcx=null, _perfCorr=null;
let _perfPeriod = localStorage.getItem('sepi_period') || "ITD";   // 1M | 3M | YTD | ITD
let _perfStart = "";       // comparison start override (date picker); "" = first exposure

// Stats of a normalized/NAV series, mirroring the backend's series_stats (client-side so the
// period tabs can re-slice the since-inception curve without another round-trip).
function statsOf(navs){
  const n=(navs||[]).length;
  if(n<2||!navs[0]) return {total:null,ann:null,vol:null,sharpe:null,mdd:0,n};
  const rets=[]; for(let i=1;i<n;i++){ if(navs[i-1]) rets.push(navs[i]/navs[i-1]-1); }
  const total=navs[n-1]/navs[0]-1;
  const ann=rets.length?Math.pow(navs[n-1]/navs[0],252/rets.length)-1:null;
  let vol=null,sharpe=null;
  if(rets.length>1){ const m=rets.reduce((a,b)=>a+b,0)/rets.length;
    const sd=Math.sqrt(rets.reduce((a,b)=>a+(b-m)*(b-m),0)/(rets.length-1)); vol=sd*Math.sqrt(252);
    sharpe=(vol&&ann!=null)?ann/vol:null; }
  let peak=navs[0],mdd=0; navs.forEach(v=>{ peak=Math.max(peak,v); if(peak) mdd=Math.min(mdd,v/peak-1); });
  return {total,ann,vol,sharpe,mdd,n};
}
// Heatmap cell colour (green +, red −) with intensity scaled to ``sc`` — the mock's calendar/YTD shade.
function heat(v,sc){ if(v==null) return "var(--panel-2)"; const t=Math.max(-1,Math.min(1,v/sc));
  return t>=0?`rgba(95,176,136,${(0.1+t*0.55).toFixed(2)})`:`rgba(207,111,102,${(0.1-t*0.55).toFixed(2)})`; }

async function loadPerformance(){
  // Growth + risk default to "since first exposure" (server-side: the first snapshot that
  // actually holds positions — cash-only days before the first rebalance are excluded);
  // the date picker (_perfStart → ?start=) re-bases everything, benchmarks included.
  const w = _perfStart ? "?start=" + _perfStart : "";
  const [tr, slip, risk, st, fees, rcx, corr, pl, tca] = await Promise.all(
    ["/api/track_record"+w,"/api/slippage","/api/risk"+w,"/api/state","/api/fees",
     "/api/risk_contrib","/api/correlation","/api/premium_ledger","/api/tca"].map(get));
  lastFetch = Date.now(); tickFreshness();
  _perfTR=tr; _perfSlip=slip; _perfFees=fees; _perfRisk=risk; _perfSt=st; _perfRcx=rcx; _perfCorr=corr;
  if(st){ pushPx(st); renderTape(st); }   // keep the pinned ticker tape fresh on the Performance tab
  renderReturns(tr, slip, fees, risk);   // Returns sub-view (period / calendar / sharpe / annual / ribbon / growth / bench)
  renderPremiumLedger(pl);               // realized option income (C4)
  renderRisk(risk, st, rcx, corr);       // Risk sub-view (contrib+corr / dist / ribbon / drawdown / rolling vol)
  renderRollingBeta(risk);               // realized beta vs SPY (C4)
  renderAttribution(st);                 // Attribution sub-view — today's P&L by holding / sector
  renderExecution(slip, fees);           // Execution sub-view
  renderTCA(tca);                        // per-cycle execution cost analysis (C4)
}
function setPerfPeriod(p){ _perfPeriod=p; localStorage.setItem('sepi_period', p); if(_perfTR) renderPeriodPanel(_perfTR, _perfRisk); }
function setPerfStart(v){ _perfStart = v || ""; loadPerformance(); }

// Today's P&L attribution: each holding's dollar contribution to the day = market value × today's
// return (day%). Exact intraday decomposition; the bars sum to the day's equity P&L. Rolled up by
// sector too. Needs the live day% (from the price feed) — degrades to a wait-note when absent.
function attrContribs(st){
  return (st && st.positions || [])
    .filter(r => r.symbol && !_isOpt(r.symbol) && r.day_pct!=null && r.market_value!=null)
    // contribution = mv − prior-close value = mv − mv/(1+day%) = mv·day%/(1+day%)
    .map(r => ({sym:r.symbol, pnl: r.market_value * r.day_pct/(1+r.day_pct),
                sector:(REF[r.symbol.toUpperCase()]||{}).sector || "—"}));
}
function attrBar(label, pnl, max, sub){
  const w = max>0 ? Math.min(100, Math.abs(pnl)/max*100) : 0;
  const pos = pnl>=0;
  return `<div class="attr-row"><div class="attr-lbl">${label}</div>`
    + `<div class="attr-track"><span class="attr-fill ${pos?'pos':'neg'}" style="width:${w.toFixed(1)}%"></span></div>`
    + `<div class="attr-val ${sgn(pnl)}">${susd(pnl)}${sub?`<span class="attr-sub">${sub}</span>`:""}</div></div>`;
}
function renderAttribution(st){
  const c = attrContribs(st);
  if(!c.length){
    const note = '<div class="se-empty">Today\'s attribution appears once live prices are streaming (each holding\'s day P&L vs the prior close). It\'s live during market hours.</div>';
    $("attrnames").innerHTML = note; $("attrsectors").innerHTML = ""; $("attrsub").textContent = ""; $("attrsecsub").textContent = ""; return;
  }
  const total = c.reduce((a,x)=>a+x.pnl, 0);
  const max = Math.max(...c.map(x=>Math.abs(x.pnl)), 1e-9);
  c.sort((a,b)=>b.pnl-a.pnl);                                   // biggest winners first, losers last
  $("attrsub").textContent = `day total ${susd(total)} · ${c.length} holdings`;
  $("attrnames").innerHTML = c.map(x=>attrBar(symCell(x.sym), x.pnl, max)).join("");
  // sector rollup
  const bySec = {};
  c.forEach(x => { bySec[x.sector] = (bySec[x.sector]||0) + x.pnl; });
  const secs = Object.entries(bySec).map(([sector,pnl])=>({sector,pnl})).sort((a,b)=>b.pnl-a.pnl);
  const smax = Math.max(...secs.map(s=>Math.abs(s.pnl)), 1e-9);
  $("attrsecsub").textContent = `${secs.length} sector${secs.length>1?'s':''}`;
  $("attrsectors").innerHTML = secs.map(s=>attrBar(
    `<span style="display:inline-flex;align-items:center;gap:7px"><i style="width:9px;height:9px;border-radius:2px;background:${secColor(s.sector)}"></i>${s.sector}</span>`,
    s.pnl, smax)).join("");
}

// ---- Events tab: upcoming earnings / option expiries / next rebalance ------
async function loadEvents(){ renderEvents(await get("/api/events")); }
const EVT = { earnings:{label:"Earnings", cls:"warn"}, expiry:{label:"Option expiry", cls:"acc"},
              rebalance:{label:"Rebalance", cls:"mut"} };
function evtWhen(d){ return d==null?"" : d<0?"past" : d===0?"today" : d===1?"tomorrow" : `in ${d} days`; }
function renderEvents(e){
  const rows = (e && e.events) || [];
  const loading = !!(e && e.earnings_loading);
  if(!rows.length){
    $("events").innerHTML = `<div class="se-empty">No upcoming events in the next 120 days${loading?" — loading earnings…":""}.</div>`;
    $("eventssub").textContent = loading ? "loading earnings…" : "";
    return;
  }
  $("eventssub").textContent = `${rows.length} in the next 120 days${loading?" · loading earnings…":""}`;
  $("events").innerHTML = '<div class="evtlist">' + rows.map(r => {
    const t = EVT[r.type] || {label:r.type, cls:"mut"};
    const sym = r.symbol ? tkrChip(r.symbol, (REF[r.symbol.toUpperCase()]||{}).sector) : "";
    return `<div class="evt"><div class="evt-when"><div class="evt-date">${mdy(r.date)}</div>`
      + `<div class="evt-in">${evtWhen(r.days_until)}</div></div>`
      + `<div class="evt-body"><span class="evt-type ${t.cls}">${t.label}</span> ${sym}`
      + `<span class="evt-detail">${r.detail||""}</span></div></div>`;
  }).join("") + '</div>';
}

// ---- Returns sub-view (design mock: period / calendar / sharpe+annual / ribbon / growth / bench) --
function renderReturns(tr, slip, fees, risk){
  if(!tr || !tr.available){ $("pr-empty").style.display="block"; $("pr-body").style.display="none"; return; }
  $("pr-empty").style.display="none"; $("pr-body").style.display="flex";
  renderPeriodPanel(tr, risk);
  $("pr-calendar").innerHTML = calHtml(tr.monthly);
  $("pr-sharpe").innerHTML   = sharpeStripHtml(risk);
  $("pr-annual").innerHTML   = annualHtml(tr);
  renderReturnsRibbon(tr, slip);
  renderReturnsTrack(tr);
  renderBenchTable(tr);
}

// Slice the since-inception NAV curve to a period (1M/3M/YTD/ITD) — the mock's in-panel selector.
function sliceNav(dates, navs, period){
  if(period==="ITD" || !(dates||[]).length) return (navs||[]).slice();
  const d=new Date(); let cut;
  if(period==="YTD") cut=`${d.getFullYear()}-01-01`;
  else { const c=new Date(d); c.setMonth(c.getMonth()-(period==="1M"?1:3)); cut=c.toISOString().slice(0,10); }
  const out=[]; for(let i=0;i<dates.length;i++){ if(String(dates[i])>=cut) out.push(navs[i]); }
  return out.length>=2?out:(navs||[]).slice(-2);
}
// Period-performance panel: 4 KPI cells (Return/Sharpe/Max-DD/Volatility) for the selected lookback
// + a spark, computed client-side off the since-inception curve (mock's Period performance).
function renderPeriodPanel(tr, risk){
  const host=$("pr-period"); if(!host) return;
  // Never rebuild while the date picker is in use (the 30s tick used to destroy a focused
  // input mid-pick, which could commit unintended values), and skip identical repaints.
  if (host.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const sig = JSON.stringify([_perfPeriod, _perfStart, tr.start, tr.days, tr.nav_now]);
  if (host._sig === sig) return;
  host._sig = sig;
  const slice=sliceNav(tr.dates, tr.nav, _perfPeriod), m=statsOf(slice), imm=slice.length<11;
  const per=_perfPeriod;
  const tabBtns=["1M","3M","YTD","ITD"].map(p=>`<button onclick="setPerfPeriod('${p}')" style="background:${p===per?'var(--panel-2)':'transparent'};border:0;border-radius:5px;padding:4px 13px;font:600 11px 'Space Grotesk';color:${p===per?'var(--fg)':'var(--muted)'};cursor:pointer">${p}</button>`).join("");
  const kc=(label,val,color,sub)=>`<div style="background:var(--panel);padding:12px 16px"><div style="font:600 10px 'Space Grotesk';letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">${label}</div><div style="font:500 21px var(--mono);margin-top:7px;color:${color};font-variant-numeric:tabular-nums">${val}</div><div style="font:400 10px var(--mono);margin-top:4px;color:var(--muted)">${sub}</div></div>`;
  const cells=[
    kc("Return", spct(m.total), m.total==null?'var(--fg)':(m.total>=0?'var(--green)':'var(--red)'), per+" · net"),
    kc("Sharpe"+(imm?"*":""), m.sharpe!=null?m.sharpe.toFixed(2):"—", 'var(--fg)', imm?"needs ≥10 days":"annualized"),
    kc("Max drawdown", pct(m.mdd), 'var(--red)', "peak-to-trough"),
    kc("Volatility"+(imm?"*":""), m.vol!=null?pct(m.vol):"—", 'var(--fg)', imm?"needs ≥10 days":"annualized"),
  ].join("");
  const evs=tr.events||[], _lastEv=t=>{ const f=evs.filter(e=>t?e.type===t:e.type!=="rebalance"); return f.length?f[f.length-1].date:null; };
  const presets=[["exposure", tr.exposure_start],["last rebal", _lastEv("rebalance")],["last action", _lastEv(null)]]
    .filter(([,d],i,a)=>d && a.findIndex(x=>x[1]===d)===i)
    .map(([l,d])=>`<button onclick="setPerfStart('${d}')" data-tip="compare from ${d}" style="background:none;border:1px solid var(--line);border-radius:6px;color:var(--muted);font:500 10px 'Space Grotesk';padding:3px 7px;cursor:pointer">${l}</button>`).join("");
  const dateCtl=`<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px">
    <span style="font:400 10px 'Space Grotesk';color:var(--muted)" data-tip="Comparison start date — defaults to the first day the book held positions (cash-only days before the first rebalance are excluded). Re-bases the curve, stats and every benchmark.">compare from</span>
    ${presets}
    <input type="date" value="${_perfStart || tr.start || tr.inception || ''}" onchange="setPerfStart(this.value)"
      style="background:var(--bg);border:1px solid var(--line);border-radius:6px;color:var(--fg);font:500 11px var(--mono);padding:3px 6px;color-scheme:dark">
    ${_perfStart?`<button onclick="setPerfStart('')" title="back to first exposure" style="background:none;border:1px solid var(--line);border-radius:6px;color:var(--muted);font:600 10px var(--mono);padding:3px 7px;cursor:pointer">RESET</button>`:''}</span>`;
  host.innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Period performance${srcInfo('navcurve')}</span>`
    +`<span style="display:inline-flex;align-items:center">${dateCtl}<span style="display:inline-flex;background:var(--bg-2);border:1px solid var(--line);border-radius:8px;padding:2px">${tabBtns}</span></span></div>`
    +`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line-soft)">${cells}</div>`
    +`<div style="padding:8px 10px 10px">${periodSparkSvg(slice)}</div></div>`;
}
function periodSparkSvg(navs){
  const a=(navs||[]).filter(v=>v!=null);
  if(a.length<2) return '<div class="se-empty" style="height:80px;display:flex;align-items:center;justify-content:center">appears after a second day</div>';
  const W=500,H=80,p=4, d=sparkP(a,W,H,p), up=a[a.length-1]>=a[0], c=up?'#5fb088':'#cf6f66';
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block;height:80px"><defs><linearGradient id="psg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".2"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient></defs><path d="${d} L${W-p} ${H-p} L${p} ${H-p} Z" fill="url(#psg)"/><path d="${d}" fill="none" stroke="${c}" stroke-width="1.8" stroke-linejoin="round"/></svg>`;
}

// Monthly-returns calendar (mock: 54px year · 12 months · YTD, green/red heat cells).
function calHtml(monthly){
  const cells=(monthly||[]).filter(c=>c.ret!=null);
  const gc="54px repeat(12,1fr) 64px";
  const body=(()=>{
    if(!cells.length) return '<div class="se-empty">monthly returns appear once a calendar month of live history accumulates</div>';
    const byYear={}; (monthly||[]).forEach(c=>{ (byYear[c.year]||(byYear[c.year]={}))[c.month]=c; });
    const years=Object.keys(byYear).map(Number).sort();
    let head=`<div style="display:grid;grid-template-columns:${gc};gap:3px;margin-bottom:3px"><span></span>`
      +MON.map(m=>`<span style="text-align:center;font:600 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:var(--muted)">${m}</span>`).join("")
      +`<span style="text-align:center;font:600 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:#8295ab">YTD</span></div>`;
    const rows=years.map(y=>{
      let ytd=1, any=false, cs="";
      for(let m=1;m<=12;m++){ const c=byYear[y][m];
        if(c && c.ret!=null){ ytd*=(1+c.ret); any=true;
          const prov=c.days<5?'<span style="color:var(--muted)">·</span>':'';
          cs+=`<div title="${MON[m-1]} ${y}: ${spct(c.ret)} · ${c.days}d${c.days<5?' partial':''}" style="height:38px;border-radius:4px;background:${heat(c.ret,0.06)};display:flex;align-items:center;justify-content:center;font:500 10.5px var(--mono);color:var(--fg)">${(c.ret*100).toFixed(1)}${prov}</div>`;
        } else cs+='<div style="height:38px;border-radius:4px;background:#0c1626"></div>'; }
      const yv=any?ytd-1:null;
      return `<div style="display:grid;grid-template-columns:${gc};gap:3px;margin-bottom:3px"><span style="display:flex;align-items:center;font:600 12px var(--mono);color:var(--fg-dim)">${y}</span>${cs}<div style="height:38px;border-radius:4px;background:${heat(yv,0.3)};display:flex;align-items:center;justify-content:center;font:600 11px var(--mono);color:var(--fg);border:1px solid var(--line)">${yv==null?'':(yv*100).toFixed(1)+'%'}</div></div>`;
    }).join("");
    return `<div>${head}${rows}</div>`;
  })();
  const sub=cells.length?`${cells.length} month${cells.length>1?'s':''} · green up / red down · “·” partial`:"% per month · net";
  return ovPanel("Monthly returns", sub, body, "13px 16px", "navcurve");
}

// Rolling Sharpe strip (mock: target band 1.0–1.5) fed the real rolling-Sharpe series.
function sharpeStripHtml(risk){
  const arr=(risk&&risk.rolling_sharpe)||[], win=(risk&&risk.rolling_window)||10;
  const idx=[]; arr.forEach((v,i)=>{ if(v!=null) idx.push(i); });
  const body=(()=>{
    if(idx.length<2) return `<div class="se-empty" style="flex:1;min-height:200px;display:flex;align-items:center;justify-content:center;text-align:center">rolling Sharpe appears once a ${win}-day window accumulates (needs ≥10 days of history)</div>`;
    const vals=idx.map(i=>arr[i]);
    const W=508,H=232,pl=34,pr=12,pt=14,pb=26,pw=W-pl-pr,ph=H-pt-pb;
    const hi=Math.max(2,...vals), lo=Math.min(0,...vals);
    const X=k=>pl+(idx.length<=1?0:k/(idx.length-1)*pw), Y=v=>pt+(hi-v)/(hi-lo)*ph;
    const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),acc=cssv('--accent','#46b8ad');
    let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:232px">`;
    g+=`<rect x="${pl}" y="${Y(1.5).toFixed(1)}" width="${pw}" height="${(Y(1.0)-Y(1.5)).toFixed(1)}" fill="rgba(70,184,173,.07)"/>`;
    [0,0.5,1,1.5,2].forEach(v=>{ if(v>hi||v<lo) return; const y=Y(v).toFixed(1); g+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-6}" y="${(+y+3).toFixed(1)}" text-anchor="end" font-size="10" fill="${mut}" font-family="IBM Plex Mono">${v.toFixed(1)}</text>`; });
    if(1>=lo && 1<=hi){ const y1=Y(1).toFixed(1); g+=`<line x1="${pl}" y1="${y1}" x2="${W-pr}" y2="${y1}" stroke="${acc}" stroke-dasharray="3 3" stroke-opacity=".5"/>`; }
    const d=vals.map((v,k)=>(k?"L":"M")+X(k).toFixed(1)+" "+Y(v).toFixed(1)).join(" ");
    g+=`<path d="${d}" fill="none" stroke="${acc}" stroke-width="1.8" stroke-linejoin="round"/><circle cx="${X(idx.length-1).toFixed(1)}" cy="${Y(vals[vals.length-1]).toFixed(1)}" r="3" fill="${acc}"/></svg>`;
    const cur=`<div style="display:flex;align-items:baseline;gap:8px;padding:0 2px 8px"><span style="font:500 22px var(--mono);color:var(--fg)">${vals[vals.length-1].toFixed(2)}</span><span style="font:400 10.5px 'Space Grotesk';color:var(--muted)">rolling ${win}-day · target band 1.0–1.5</span></div>`;
    return cur+g;
  })();
  return ovPanel("Rolling Sharpe","annualized", body, "12px 14px 10px", "risk", true);
}

// Per-calendar-year returns from a daily series (chained off the prior year-end).
function annualRets(dates, series){
  const byY={}; (dates||[]).forEach((d,i)=>{ const y=String(d).slice(0,4); (byY[y]||(byY[y]=[])).push(series[i]); });
  const years=Object.keys(byY).sort(); const out=[]; let prevEnd=null;
  years.forEach(y=>{ const v=byY[y], base=prevEnd!=null?prevEnd:v[0]; out.push({y:+y, r:base?v[v.length-1]/base-1:null}); prevEnd=v[v.length-1]; });
  return out;
}
// Annual bars: strategy vs SPY (mock annualHtml), from the real curves.
function annualHtml(tr){
  const sp=(tr.benchmarks&&tr.benchmarks.SPY)?tr.benchmarks.SPY.norm:null;
  const sa=annualRets(tr.dates, tr.nav), spm={}; (sp?annualRets(tr.dates, sp):[]).forEach(x=>spm[x.y]=x.r);
  const A=sa.map(x=>({y:x.y, strat:x.r, spy:spm[x.y]}));
  const body=(()=>{
    if(!A.length) return '<div class="se-empty" style="flex:1;min-height:200px;display:flex;align-items:center;justify-content:center;text-align:center">annual returns appear once a full year of history accumulates</div>';
    const W=508,H=270,pl=34,pr=10,pt=12,pb=40,pw=W-pl-pr,ph=H-pt-pb;
    const all=A.flatMap(a=>[a.strat||0,a.spy||0]); const hi=Math.max(0.05,...all), lo=Math.min(-0.05,...all);
    const Y=v=>pt+(hi-v)/(hi-lo)*ph, gw=pw/A.length, bw=Math.min(gw*0.32,40);
    const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c');
    let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:270px">`;
    [hi,(hi+lo)/2,0,lo].forEach(v=>{ const y=Y(v).toFixed(1); g+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-6}" y="${(+y+3).toFixed(1)}" text-anchor="end" font-size="10" fill="${mut}" font-family="IBM Plex Mono">${(v*100).toFixed(0)}</text>`; });
    const y0=Y(0);
    A.forEach((a,i)=>{ const cx=pl+gw*i+gw/2;
      [[a.strat||0,'#46b8ad',-1],[a.spy||0,'#5a6781',1]].forEach(arr=>{ const v=arr[0],c=arr[1],s=arr[2]; const x=cx+s*bw/2-bw/2, yy=Y(v), top=Math.min(yy,y0), hh=Math.abs(yy-y0); g+=`<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1,hh).toFixed(1)}" rx="1.5" fill="${c}"/>`; });
      g+=`<text x="${cx.toFixed(1)}" y="${H-22}" text-anchor="middle" font-size="10" fill="${mut}" font-family="IBM Plex Mono">'${String(a.y).slice(2)}</text>`; });
    g+="</svg>";
    const leg=`<div style="display:flex;gap:16px;justify-content:center;padding-top:2px"><span style="display:inline-flex;align-items:center;gap:6px;font:400 10.5px 'Space Grotesk';color:var(--fg-dim)"><span style="width:12px;height:3px;background:#46b8ad;border-radius:2px"></span>Strategy</span><span style="display:inline-flex;align-items:center;gap:6px;font:400 10.5px 'Space Grotesk';color:#aab4c3"><span style="width:12px;height:3px;background:#5a6781;border-radius:2px"></span>SPY</span></div>`;
    return g+leg;
  })();
  return ovPanel("Annual returns","strategy vs SPY", body, "12px 14px 12px", "benchmarks", true);
}

// 6-col ribbon (mock): Days live · Since inception · Ann return · Live Sharpe · Premium · Avg slippage.
function renderReturnsRibbon(tr, slip){
  const star=tr.mature?"":"*";
  const avgBps=slip&&slip.avg_slippage_bps!=null?(slip.avg_slippage_bps>0?"+":"")+slip.avg_slippage_bps.toFixed(1)+" bps":"—";
  const cells=[
    kpiCell("Days live", fmt(tr.days), "since "+(tr.inception||"—")),
    kpiCell("Since inception", spct(tr.total_return), "paper, net of costs", {valColor:tr.total_return==null?'var(--fg)':(tr.total_return>=0?'var(--green)':'var(--red)')}),
    kpiCell("Ann. return"+star, tr.mature?spct(tr.ann_return):"—", tr.mature?"annualized":"needs ≥10 days"),
    kpiCell("Live Sharpe"+star, tr.mature&&tr.sharpe!=null?tr.sharpe.toFixed(2):"—", tr.mature?"annualized":"needs ≥10 days"),
    kpiCell("Premium collected", usd(tr.premium_collected), "SPY overlay"),
    kpiCell("Avg slippage", avgBps, "vs intended price"),
  ].join("");
  $("pr-ribbon").innerHTML=ribWrap(`<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden">${cells}</div>`, "kpiret");
}

// Growth chart (mock multiLine) — strategy teal + benchmarks neutral/purple/blue.
function perfMultiLine(dates, series, events){
  let all=[]; series.forEach(se=>se.data.forEach(v=>{ if(v!=null) all.push(v); }));
  if(all.length<2) return '<div class="se-empty">one data point so far — the curve appears after a second day</div>';
  const W=1160,H=260,pl=58,pr=16,pt=14,pb=28,pw=W-pl-pr,ph=H-pt-pb;
  let lo=Math.min(...all),hi=Math.max(...all); const pad=(hi-lo)*0.1||0.01; lo-=pad; hi+=pad;
  const n=dates.length,X=i=>pl+(n<=1?0:i/(n-1)*pw),Y=v=>pt+(hi-v)/(hi-lo)*ph;
  const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),ML="font-family:var(--mono)";
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block;height:260px">`;
  for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4,y=Y(v).toFixed(1); s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-8}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${mut}" style="${ML}">${((v-1)*100).toFixed(0)}%</text>`; }
  // Action markers: dashed verticals at every rebalance / console action, so "what did that
  // trade do to the curve" is visible instead of mental (hover the dot for the label).
  (events||[]).forEach(ev=>{ const i=dates.findIndex(d=>d>=ev.date); if(i<0) return; const x=X(i).toFixed(1);
    const c=ev.type==='rebalance'?'#8f7ee0':'#d8a84b';
    s+=`<line x1="${x}" y1="${pt}" x2="${x}" y2="${H-pb}" stroke="${c}" stroke-dasharray="3 4" stroke-opacity=".5"/>`
      +`<circle cx="${x}" cy="${pt+4}" r="4" fill="${c}" data-tip="${ev.date} — ${esc(ev.label)}" style="cursor:help"/>`; });
  const step=Math.max(1,Math.round(n/7));
  for(let i=0;i<n;i+=step) s+=`<text x="${X(i).toFixed(1)}" y="${H-9}" text-anchor="middle" font-size="10.5" fill="${mut}" style="${ML}">${String(dates[i]).slice(5)}</text>`;
  series.forEach(se=>{ let p=""; se.data.forEach((v,i)=>{ if(v!=null) p+=(p?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1); }); s+=`<path d="${p}" fill="none" stroke="${se.color}" stroke-width="${se.w||1.6}" stroke-linejoin="round"/>`; });
  return s+"</svg>";
}
function renderReturnsTrack(tr){
  const series=[{data:tr.norm,color:cssv('--accent','#46b8ad'),w:2.2,name:"Strategy"}];
  const bsyms=Object.keys(tr.benchmarks||{});
  bsyms.forEach((sym,i)=>series.push({data:tr.benchmarks[sym].norm,color:bcol(sym,i),w:1.6,name:sym}));
  const legend=series.map(s=>`<span style="display:inline-flex;align-items:center;gap:6px;font:400 11.5px 'Space Grotesk';color:var(--fg-dim)"><span style="width:14px;height:3px;border-radius:2px;background:${s.color}"></span>${s.name}</span>`).join("");
  const evLegend=(tr.events&&tr.events.length)?`<span style="font:400 10.5px 'Space Grotesk';color:var(--muted);display:inline-flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#8f7ee0"></span>rebalance <span style="width:8px;height:8px;border-radius:50%;background:#d8a84b;margin-left:6px"></span>manual action</span>`:"";
  $("pr-track").innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Growth since inception — strategy vs benchmarks</span><span class="ovhs">${tr.dates[0]} → ${tr.dates[tr.dates.length-1]} <button class="exm-chip" onclick="exportTrackCSV()">⤓ csv</button>${srcInfo('benchmarks')}</span></div><div style="display:flex;gap:18px;padding:11px 18px 2px;flex-wrap:wrap;align-items:center">${legend}${evLegend}</div><div style="padding:6px 8px 10px">${perfMultiLine(tr.dates,series,tr.events)}</div></div>`;
}
// Benchmark comparison table (mock grid) + descriptions.
function renderBenchTable(tr){
  const bsyms=Object.keys(tr.benchmarks||{}), gc="1.6fr .8fr .8fr .8fr .8fr .8fr";
  const head=`<div style="display:grid;grid-template-columns:${gc};gap:10px;padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Series</span><span style="text-align:right">Return</span><span style="text-align:right">Ann.</span><span style="text-align:right">Vol</span><span style="text-align:right">Sharpe</span><span style="text-align:right">Max DD</span></div>`;
  const rowH=(name,swatch,m)=>`<div style="display:grid;grid-template-columns:${gc};gap:10px;align-items:center;padding:9px 16px;border-bottom:1px solid var(--line-soft)"><span style="display:flex;align-items:center;gap:9px;font:500 12.5px 'Space Grotesk';color:var(--fg)"><span style="width:10px;height:3px;border-radius:2px;background:${swatch};flex:none"></span>${name}</span><span style="text-align:right;font:500 12px var(--mono);color:${m.total_return==null?'var(--fg)':(m.total_return>=0?'var(--green)':'var(--red)')}">${spct(m.total_return)}</span><span style="text-align:right;font:400 12px var(--mono);color:var(--fg-dim)">${tr.mature&&m.ann_return!=null?spct(m.ann_return):"—"}</span><span style="text-align:right;font:400 12px var(--mono);color:var(--fg-dim)">${m.ann_vol!=null?pct(m.ann_vol):"—"}</span><span style="text-align:right;font:400 12px var(--mono);color:var(--fg-dim)">${tr.mature&&m.sharpe!=null?m.sharpe.toFixed(2):"—"}</span><span style="text-align:right;font:400 12px var(--mono);color:${m.max_drawdown==null?'var(--fg-dim)':'var(--red)'}">${pct(m.max_drawdown)}</span></div>`;
  let rows=rowH("Strategy (this book)", cssv('--accent','#46b8ad'), tr);
  bsyms.forEach((sym,i)=>rows+=rowH(tr.benchmarks[sym].name||sym, bcol(sym,i), tr.benchmarks[sym]));
  let note="";
  if(!tr.mature) note+=`<div style="padding:8px 16px 0;font:400 11px var(--mono);color:var(--muted)">* Annualized return &amp; Sharpe appear once ≥10 trading days accumulate (currently ${tr.days}).</div>`;
  if(!bsyms.length) note+=`<div style="padding:8px 16px 0;font:400 11px var(--mono);color:var(--muted)">Benchmarks unavailable (data feed offline) — retries next load.</div>`;
  let desc="";
  if(bsyms.length){ desc=`<div style="padding:12px 16px 14px"><div style="font:500 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin-bottom:7px">What these benchmarks are</div>`
    +bsyms.map((sym,i)=>`<div style="font:400 11.5px/1.55 'Space Grotesk';color:var(--muted);margin-bottom:4px"><b style="color:${bcol(sym,i)};font-weight:600">${tr.benchmarks[sym].name||sym}</b> — ${tr.benchmarks[sym].desc||""}</div>`).join("")+`</div>`; }
  $("pr-bench").innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Benchmark comparison</span><span class="ovhs">paper, since go-live${srcInfo('benchmarks')}</span></div>${head}${rows}${note}${desc}</div>`;
}

// ---- Execution sub-view (mock: slippage grid) + fees (SFI extra) ----------------
function renderExecution(slip, fees){
  const gc="2fr .6fr .7fr 1fr 1fr .9fr .9fr";
  let slipBody;
  if(slip && slip.n_fills){
    const head=`<div style="display:grid;grid-template-columns:${gc};gap:10px;padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Instrument</span><span>Side</span><span style="text-align:right">Qty</span><span style="text-align:right">Intended</span><span style="text-align:right">Filled</span><span style="text-align:right">Slippage</span><span style="text-align:right">Cost</span></div>`;
    const rows=slip.fills.map(r=>{ const bpsPos=r.slippage_bps>0, costPos=r.slippage_usd>0;
      const ref=REF[String(r.symbol).toUpperCase()]||{}, sec=ref.sector, nm=ref.name||r.symbol;
      return `<div style="display:grid;grid-template-columns:${gc};gap:10px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--line-soft)">`
        +`<span style="display:flex;align-items:center;gap:10px;min-width:0" data-tip="${esc(r.type?('order type: '+r.type):'')}">${tkrChip(r.symbol,sec)}<span style="font:500 12px 'Space Grotesk';color:var(--fg-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${nm}</span></span>`
        +`<span style="font:400 11px 'Space Grotesk';color:var(--muted);text-transform:uppercase">${r.side}</span>`
        +`<span style="text-align:right;font:400 12px var(--mono);color:var(--fg-dim)">${fmt(r.qty)}</span>`
        +`<span style="text-align:right;font:400 12px var(--mono);color:var(--fg-dim)">${usd2(r.intended)}</span>`
        +`<span style="text-align:right;font:400 12px var(--mono);color:var(--fg)">${usd2(r.filled)}</span>`
        +`<span style="text-align:right;font:500 12px var(--mono);color:${bpsPos?'var(--red)':'var(--green)'}">${bpsPos?'+':''}${r.slippage_bps} bps</span>`
        +`<span style="text-align:right;font:500 12px var(--mono);color:${costPos?'var(--red)':'var(--green)'}">${susd2(-r.slippage_usd)}</span></div>`; }).join("");
    slipBody=head+rows;
  } else {
    slipBody='<div class="se-empty" style="padding:20px 16px">No filled orders yet — this compares each fill to its reference price (the order’s limit, or the arrival mid for market orders) once there’s trading activity.</div>';
  }
  const slipSub=slip&&slip.n_fills?`${slip.n_fills} filled order${slip.n_fills>1?'s':''} · +bps = paid up vs reference`:"vs intended price";
  $("ex-slip").innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Execution quality / slippage</span><span class="ovhs">${slipSub}${srcInfo('slippage')}</span></div>${slipBody}</div>`;

  // Fees: regulatory / broker charges (CAT, TAF, SEC, …) from Alpaca activities — an SFI extra.
  const allIn=(fees&&fees.total_usd||0)+(slip&&slip.total_slippage_usd>0?slip.total_slippage_usd:0);
  let feeBody, feeSub;
  if(fees && fees.n){
    feeSub=`${usd2(fees.total_usd)} total · ${fees.n} charge${fees.n>1?'s':''} · all-in ${usd2(allIn)}`;
    const chips=Object.entries(fees.by_type).map(([t,v])=>`<span title="${t} fees" style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px;background:var(--panel-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:11px;color:var(--muted)">${t} <b style="color:var(--fg);font-family:var(--mono)">${usd2(v)}</b></span>`).join("");
    feeBody=`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">${chips}</div>`+table(fees.items,[
      {h:"Date", tip:"When the fee was booked (Alpaca books fees the morning after the trade).", f:r=>r.date||"—"},
      {h:"Type", tip:"CAT = Consolidated Audit Trail · TAF = FINRA Trading Activity Fee · REG/SEC = Section 31.", f:r=>r.type},
      {h:"Amount", tip:"Fee charged (a debit to the account).", f:r=>`<span class="neg">-${usd2(r.amount)}</span>`},
      {h:"Description", cls:()=>"mut", f:r=>r.description||"—"},
    ]);
  } else {
    feeSub=""; feeBody='<div class="se-empty" style="padding:20px 16px">No fees yet. Regulatory fees (CAT, TAF, SEC Section 31) appear here the morning after a trade — tiny (often rounded to $0.01) but real, and already in NAV.</div>';
  }
  $("ex-fees").innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Fees</span><span class="ovhs">${feeSub}${srcInfo('fees')}</span></div><div style="padding:12px 16px">${feeBody}</div></div>`;
}

// ---- Risk sub-view (mock: contrib+corr / distribution / ribbon / drawdown / rolling vol) --------
function renderRisk(risk, st, rcx, corr){
  if(!risk || !risk.available){ $("rk-empty").style.display="block"; $("rk-body").style.display="none"; return; }
  $("rk-empty").style.display="none"; $("rk-body").style.display="flex";
  $("rk-contrib").innerHTML = riskcHtml(rcx);
  $("rk-corr").innerHTML    = corrHtml(corr);
  $("rk-dist").innerHTML    = distHtml(risk);
  renderRiskRibbon(risk, st);
  $("rk-dd").innerHTML = underwaterHtml(risk);
  $("rk-rv").innerHTML = rollVolHtml(risk);
}

// Risk contribution (mock riskcHtml): each holding's share of portfolio variance as a sector-tinted bar.
function riskcHtml(rcx){
  const body=(()=>{
    if(!rcx || !rcx.available || !(rcx.names||[]).length) return '<div class="se-empty">Risk contribution appears after the next rebalance — the engine records each holding’s share of portfolio variance (from the covariance matrix) each rebalance.</div>';
    const mx=Math.max(...rcx.names.map(r=>r.rc_pct),0.01);
    return rcx.names.map(r=>{ const c=secColor((REF[String(r.symbol).toUpperCase()]||{}).sector);
      const rw=r.weight?r.rc_pct/r.weight:null, tip=`risk ${pct(r.rc_pct)} · weight ${pct(r.weight)}${rw!=null?` · ${rw.toFixed(2)}× risk/wt`:""}`;
      return `<div title="${esc(tip)}" style="display:grid;grid-template-columns:54px 1fr 46px;gap:10px;align-items:center;padding:6px 0"><span style="font:600 11px var(--mono);color:var(--fg-dim)">${r.symbol}</span><span style="height:14px;border-radius:3px;background:#0c1626;overflow:hidden;display:block"><span style="display:block;height:100%;width:${(r.rc_pct/mx*100).toFixed(1)}%;background:${c};border-radius:3px"></span></span><span style="text-align:right;font:500 11px var(--mono);color:#aab4c3">${(r.rc_pct*100).toFixed(1)}%</span></div>`; }).join("");
  })();
  const sub=(rcx&&rcx.portfolio_vol!=null)?`vol ${pct(rcx.portfolio_vol)} · as of ${String(rcx.ts).slice(0,10)}`:"% of portfolio variance";
  return ovPanel("Risk contribution", sub, body, "10px 15px 12px", "riskcontrib");
}

// Correlation matrix (mock corrHtml): pairwise trailing-60d return correlation heatmap.
function corrHtml(corr){
  const body=(()=>{
    if(!corr || !corr.available || !(corr.symbols||[]).length) return '<div class="se-empty" style="min-height:130px;display:flex;align-items:center;justify-content:center;text-align:center">correlation appears once the market-data feed returns enough daily history for the held names</div>';
    const syms=corr.symbols, n=syms.length, W=508, cell=(W-70)/n, H=70+cell*n, lab="#8295ab";
    let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:${H}px">`;
    syms.forEach((s,i)=>{ g+=`<text x="${(64+cell*i+cell/2).toFixed(1)}" y="14" text-anchor="middle" font-size="10" fill="${lab}" font-family="IBM Plex Mono">${s}</text><text x="58" y="${(26+cell*i+cell/2+3).toFixed(1)}" text-anchor="end" font-size="10" fill="${lab}" font-family="IBM Plex Mono">${s}</text>`; });
    for(let i=0;i<n;i++)for(let j=0;j<n;j++){ const c=corr.matrix[i][j], x=64+cell*j, y=22+cell*i;
      if(c==null){ g+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(cell-2).toFixed(1)}" height="${(cell-2).toFixed(1)}" rx="2" fill="none" stroke="#1b2740" stroke-dasharray="2 2"/>`; continue; }
      const col=c>=0?`rgba(70,184,173,${(0.06+c*0.6).toFixed(2)})`:`rgba(207,111,102,${(0.06-c*0.5).toFixed(2)})`;
      g+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(cell-2).toFixed(1)}" height="${(cell-2).toFixed(1)}" rx="2" fill="${col}"/>`;
      if(cell>40) g+=`<text x="${(x+cell/2-1).toFixed(1)}" y="${(y+cell/2+2).toFixed(1)}" text-anchor="middle" font-size="10" fill="${Math.abs(c)>0.5?'#eaf2fb':lab}" font-family="IBM Plex Mono">${c.toFixed(2)}</text>`; }
    return g+"</svg>";
  })();
  const sub=(corr&&corr.available)?`pairwise · trailing ${corr.window||60}d`:"pairwise · trailing 60d";
  return ovPanel("Correlation", sub, body, "12px 14px", "correlation");
}

// Daily-return distribution (mock distHtml): histogram, VaR line, mean marker.
function distHtml(risk){
  const a=(risk&&risk.returns||[]).filter(v=>v!=null);
  const body=(()=>{
    if(a.length<3) return '<div class="se-empty" style="height:250px;display:flex;align-items:center;justify-content:center">the return distribution appears once a few days of history accumulate</div>';
    const W=508,H=250,pl=12,pr=12,pt=14,pb=34,pw=W-pl-pr,ph=H-pt-pb;
    let lo=Math.min(...a), hi=Math.max(...a); const spanv=Math.max(hi-lo,0.001); lo-=spanv*0.06; hi+=spanv*0.06;
    const nb=Math.min(23,Math.max(7,Math.round(Math.sqrt(a.length))*2+1)), bins=new Array(nb).fill(0);
    a.forEach(v=>{ let k=Math.floor((v-lo)/(hi-lo)*nb); k=Math.max(0,Math.min(nb-1,k)); bins[k]++; });
    const mx=Math.max(...bins), bw=pw/nb;
    const sorted=a.slice().sort((x,y)=>x-y), var95=sorted[Math.floor(sorted.length*0.05)], mean=a.reduce((x,y)=>x+y,0)/a.length;
    const X=v=>pl+(v-lo)/(hi-lo)*pw, Y=c=>pt+(1-c/mx)*ph;
    const mut=cssv('--muted','#65758c'),acc=cssv('--accent','#46b8ad');
    let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:250px">`;
    bins.forEach((c,i)=>{ const x=pl+bw*i, y=Y(c), mid=lo+(i+0.5)/nb*(hi-lo); const col=mid<var95?'#cf6f66':(mid<0?'#6e4f55':'#3f7e6e'); g+=`<rect x="${(x+1).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw-2).toFixed(1)}" height="${(pt+ph-y).toFixed(1)}" rx="1.5" fill="${col}"/>`; });
    if(var95!=null){ const vx=X(var95).toFixed(1); g+=`<line x1="${vx}" y1="${pt}" x2="${vx}" y2="${pt+ph}" stroke="#cf6f66" stroke-width="1.5" stroke-dasharray="3 2"/><text x="${vx}" y="${pt-3}" text-anchor="middle" font-size="10" fill="#cf6f66" font-family="IBM Plex Mono">VaR ${(var95*100).toFixed(1)}%</text>`; }
    const mxx=X(mean).toFixed(1); g+=`<line x1="${mxx}" y1="${pt}" x2="${mxx}" y2="${pt+ph}" stroke="${acc}" stroke-width="1.5"/><text x="${mxx}" y="${pt-3}" text-anchor="middle" font-size="10" fill="${acc}" font-family="IBM Plex Mono">μ</text>`;
    for(let t=0;t<=4;t++){ const v=lo+(hi-lo)*t/4; g+=`<text x="${X(v).toFixed(1)}" y="${H-12}" text-anchor="middle" font-size="10" fill="${mut}" font-family="IBM Plex Mono">${(v*100).toFixed(1)}%</text>`; }
    return g+"</svg>";
  })();
  return ovPanel("Daily return distribution", `${a.length} daily return${a.length===1?'':'s'} · 95% VaR`, body, "12px 12px 8px", "risk");
}

// 6-col risk ribbon (mock): Max DD · Current DD · Volatility · 1-day VaR · Live Sharpe · Leverage.
function renderRiskRibbon(risk, st){
  const star=risk.mature?"":"*", cap=META.leverage_cap||2.0, lev=st?st.leverage:null, levW=lev==null?0:Math.min(100,lev/cap*100);
  const levBar=`<div style="height:4px;border-radius:2px;background:var(--panel-2);margin-top:8px;overflow:hidden"><div style="height:100%;background:var(--accent);width:${levW.toFixed(0)}%"></div></div>`;
  const dci=risk.days_in_drawdown||0;
  const cells=[
    kpiCell("Max drawdown", pct(risk.max_drawdown), "worst, on "+String(risk.max_drawdown_date).slice(5), {valColor:'var(--red)'}),
    kpiCell("Current drawdown", pct(risk.current_drawdown), dci>0?`${dci} day${dci>1?'s':''} below HWM`:"at a new high", {valColor:risk.current_drawdown<-1e-9?'var(--red)':'var(--muted)'}),
    kpiCell("Volatility"+star, risk.mature&&risk.ann_vol!=null?pct(risk.ann_vol):"—", risk.daily_vol!=null?pct(risk.daily_vol,2)+" / day":"needs ≥10 days"),
    kpiCell("1-day VaR (95%)", risk.var95_1d_pct!=null?"−"+pct(risk.var95_1d_pct):"—", risk.var95_1d_usd!=null?"≈ "+usd(risk.var95_1d_usd)+" at NAV":"parametric", {valColor:'var(--red)'}),
    kpiCell("Live Sharpe"+star, risk.mature&&risk.sharpe!=null?risk.sharpe.toFixed(2):"—", risk.mature?"annualized":"needs ≥10 days"),
    kpiCell("Leverage", lev==null?"–":lev.toFixed(2)+"×", `cap ${cap.toFixed(1)}× · gross ${usd(st?st.gross_exposure:null)}`, {extra:levBar}),
  ].join("");
  $("rk-ribbon").innerHTML=ribWrap(`<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden">${cells}</div>`, "kpirisk");
}

// Underwater / drawdown (mock underwater): 0% high-water mark at top, losses shaded below.
function underwaterHtml(risk){
  const dd=risk.drawdown||[], dates=risk.dates||[], vals=dd.filter(v=>v!=null);
  const body=(()=>{
    if(vals.length<2) return '<div class="se-empty">one data point so far — the drawdown curve appears after a second day</div>';
    const W=1160,H=200,pl=58,pr=16,pt=14,pb=26,pw=W-pl-pr,ph=H-pt-pb;
    const lo=Math.min(...vals,0), top=0, bot=lo-((0-lo)*0.12||0.001);
    const n=dates.length,X=i=>pl+(n<=1?0:i/(n-1)*pw),Y=v=>pt+(top-v)/(top-bot)*ph;
    const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),red=cssv('--red','#cf6f66'),ML="font-family:var(--mono)";
    let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block;height:200px">`;
    for(let k=0;k<=4;k++){ const v=top-(top-bot)*k/4,y=Y(v).toFixed(1); s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-8}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${mut}" style="${ML}">${(v*100).toFixed(1)}%</text>`; }
    const step=Math.max(1,Math.round(n/7));
    for(let i=0;i<n;i+=step) s+=`<text x="${X(i).toFixed(1)}" y="${H-8}" text-anchor="middle" font-size="10.5" fill="${mut}" style="${ML}">${String(dates[i]).slice(5)}</text>`;
    const y0=Y(0).toFixed(1), line=dd.map((v,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)).join(" ");
    s+=`<path d="M${X(0).toFixed(1)} ${y0} ${dd.map((v,i)=>"L"+X(i).toFixed(1)+" "+Y(v).toFixed(1)).join(" ")} L${X(n-1).toFixed(1)} ${y0} Z" fill="${red}" fill-opacity="0.13"/>`;
    s+=`<path d="${line}" fill="none" stroke="${red}" stroke-width="1.8" stroke-linejoin="round"/>`;
    return s+"</svg>";
  })();
  const sub=dates.length?`${dates[0]} → ${dates[dates.length-1]} · 0% = high-water mark`:"0% = high-water mark";
  return ovPanel("Drawdown", sub, body, "8px 8px 10px", "risk");
}

// Rolling volatility (mock rvChart): trailing-window annualized vol, amber.
function rollVolHtml(risk){
  const rv=risk.rolling_vol||[], dates=risk.dates||[], vals=rv.filter(v=>v!=null), win=risk.rolling_window||10;
  const body=(()=>{
    if(vals.length<2) return `<div class="se-empty" style="height:180px;display:flex;align-items:center;justify-content:center;text-align:center">rolling volatility appears once a ${win}-day window accumulates (needs ≥10 days of history)</div>`;
    const W=1160,H=180,pl=58,pr=16,pt=14,pb=26,pw=W-pl-pr,ph=H-pt-pb;
    let lo=Math.min(...vals),hi=Math.max(...vals); const pad=(hi-lo)*0.15||0.01; lo=Math.max(0,lo-pad); hi+=pad;
    const n=dates.length,X=i=>pl+(n<=1?0:i/(n-1)*pw),Y=v=>pt+(hi-v)/(hi-lo)*ph;
    const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),warn=cssv('--amber','#d8a84b'),ML="font-family:var(--mono)";
    let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block;height:180px">`;
    for(let k=0;k<=3;k++){ const v=lo+(hi-lo)*k/3,y=Y(v).toFixed(1); s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-8}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${mut}" style="${ML}">${(v*100).toFixed(0)}%</text>`; }
    const step=Math.max(1,Math.round(n/7));
    for(let i=0;i<n;i+=step) s+=`<text x="${X(i).toFixed(1)}" y="${H-8}" text-anchor="middle" font-size="10.5" fill="${mut}" style="${ML}">${String(dates[i]).slice(5)}</text>`;
    let p=""; rv.forEach((v,i)=>{ if(v!=null) p+=(p?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1); });
    s+=`<path d="${p}" fill="none" stroke="${warn}" stroke-width="2" stroke-linejoin="round"/>`;
    return s+"</svg>";
  })();
  return ovPanel("Rolling volatility", `${win}-day window · annualized`, body, "8px 8px 10px", "risk");
}

const esc = s => String(s).replace(/"/g,"&quot;");

// ---- data-source provenance: a hover-only ⓘ per panel (design §12 disclosure) --------------
// One entry per source of truth; each panel header carries an ⓘ whose tooltip names its origin.
const SRC = {
  live:       "Source: Alpaca account + IEX live trade feed — NAV & positions marked to the live price stream, day % vs the prior close. Falls back to the 60s snapshot when the stream is quiet.",
  alpaca:     "Source: Alpaca (broker) — live account, positions, orders & fills.",
  orders:     "Source: Alpaca — live order book (open + recent), else the engine's Postgres order log.",
  navcurve:   "Source: engine / Postgres — daily NAV snapshots written by the 60s monitor from the Alpaca account.",
  risk:       "Source: engine — risk analytics derived from the daily NAV curve (Postgres snapshots).",
  riskcontrib:"Source: engine — Euler risk decomposition (from the covariance matrix) computed at each rebalance, stored in Postgres.",
  correlation:"Source: Alpaca daily bars (IEX) — pairwise trailing-60-day return correlation across the top holdings.",
  benchmarks: "Source: engine NAV curve (Postgres) vs SPY (yfinance) and BXMD/BXRD (CBOE).",
  factors:    "Source: engine factor scores (Postgres) — computed from fundamentals/prices each rebalance.",
  targets:    "Source: engine — target weights & risk gate from the last rebalance (Postgres rebalance_log).",
  calls:      "Source: engine — SPY overlay spread legs & net credit (Postgres options_lifecycle + the live snapshot).",
  overlay:    "Source: engine — SPY beta-overwrite spread (options_lifecycle net credit + the live SPY leg positions); β overwritten = short-leg notional ÷ gross equity, live SPY spot from Alpaca.",
  premium:    "Source: engine — lifetime option premium collected (Postgres options_lifecycle).",
  quotes:     "Source: Alpaca daily bars (IEX) — OHLCV + 52-week range; the live price is overlaid on top.",
  bars:       "Source: Alpaca market data — hourly/daily bars (IEX), extended live.",
  events:     "Source: yfinance (earnings dates) + engine (option expiries & the rebalance schedule).",
  fees:       "Source: Alpaca account activities — regulatory/broker fees (CAT · TAF · SEC).",
  slippage:   "Source: Alpaca fills vs the arrival NBBO mid (Alpaca historical quotes/trades).",
  reference:  "Source: SEC company names + the SIC→GICS sector map.",
  clock:      "Source: Alpaca market clock & calendar (holiday-aware).",
  health:     "Source: engine heartbeat + Alpaca clock — freshness, rebalance schedule, drift & alerts.",
  alerts:     "Source: engine — alert log (Postgres), classified by severity.",
  attribution:"Source: engine snapshot × IEX live prices — each holding's day P&L = market value × day %.",
  kpiov:      "Source: Alpaca live account — NAV, day P&L, leverage, cash & positions; premium collected from the engine's options ledger.",
  kpiret:     "Source: engine — days live, return, annualized & Sharpe from the NAV curve, premium from the options ledger; avg slippage from Alpaca fills.",
  kpirisk:    "Source: engine — drawdown, volatility, VaR & Sharpe from the NAV curve; leverage from the live Alpaca account.",
};
function srcInfo(key){ const t = SRC[key] || key; return ` <span class="srcinfo" data-tip="${esc(t)}">ⓘ</span>`; }
// Wrap a KPI-ribbon grid (which has no header) with a corner ⓘ.
function ribWrap(gridHtml, key){ return `<div style="position:relative">${gridHtml}<span class="ribsrc" data-tip="${esc(SRC[key]||key)}">ⓘ</span></div>`; }

function table(rows, cols){
  if(!rows || !rows.length) return '<div class="se-empty">none</div>';
  const head = "<thead><tr>"+cols.map(c=>`<th${c.tip?` data-tip="${esc(c.tip)}"`:""}>${c.h}</th>`).join("")+"</tr></thead>";
  const body = "<tbody>"+rows.map(r=>"<tr>"+cols.map(c=>`<td class="${c.cls?c.cls(r):''}">${c.f(r)}</td>`).join("")+"</tr>").join("")+"</tbody>";
  return `<table class="se-table">${head}${body}</table>`;
}
function bar(frac, color){   // 0..1 micro-bar
  const w = Math.max(0, Math.min(1, frac||0))*100;
  return `<span class="se-bartrack"><span class="se-bar" style="width:${w.toFixed(0)}%;background:${color||'var(--accent)'}"></span></span>`;
}

// ---- system health panel --------------------------------------------------
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD  = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
function mdy(iso){ if(!iso) return "–"; const d=new Date(String(iso).slice(0,10)+"T00:00:00"); return MON[d.getMonth()]+" "+d.getDate(); }
function clockHM(iso){ if(!iso) return ""; const d=new Date(iso); let h=d.getHours(),m=d.getMinutes(); const ap=h<12?"am":"pm"; h=h%12||12; return h+(m?":"+String(m).padStart(2,"0"):"")+ap; }
function tsMs(s){ if(!s) return null; const t=new Date(String(s).replace(" ","T")+(/[zZ]|\+/.test(s)?"":"Z")).getTime(); return isNaN(t)?null:t; }
function ageOf(s){ const t=tsMs(s); return t==null?"–":humanAge((Date.now()-t)/1000)+" ago"; }

// ---- trading-session timer + live ET clock --------------------------------
// The US market runs on New York time; we compute the session phase in ET (via Intl, so it's
// correct regardless of the viewer's timezone) and tick a live countdown every second. MKT holds
// the last Alpaca clock (is_open + RTH boundaries), anchored to server time to shrug off clock skew.
let MKT = null;
const _etDT = new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",weekday:"short",
  hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const _etWhen = new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",weekday:"short",
  hour:"numeric",minute:"2-digit",hour12:true});
function etParts(ms){ const o={}; for(const p of _etDT.formatToParts(new Date(ms))) o[p.type]=p.value;
  return {h:(+o.hour)%24, m:+o.minute, s:+o.second, wd:o.weekday}; }
function etWhen(iso){ if(!iso) return ""; const o={}; for(const p of _etWhen.formatToParts(new Date(iso))) o[p.type]=p.value;
  return `${o.weekday} ${o.hour}:${o.minute}${(o.dayPeriod||"").toLowerCase()} ET`; }
function etNow(){ return Date.now() + (MKT ? (MKT.serverMs - MKT.fetchedAt) : 0); }
function dhm(ms){ if(ms<0) ms=0; const s=Math.floor(ms/1000), d=Math.floor(s/86400),
  h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60), sec=s%60;
  if(d>0) return `${d}d ${h}h`; if(h>0) return `${h}h ${String(m).padStart(2,"0")}m`;
  return `${m}m ${String(sec).padStart(2,"0")}s`; }
// Session phase from the ET wall clock + Alpaca's authoritative is_open (which already handles
// holidays). Extended-hours bands are ET-standard: pre 4:00–9:30, regular 9:30–16:00, after 16:00–20:00.
function sessionInfo(){
  if(!MKT) return {dot:"mut", val:"—", detail:"live clock offline"};
  const now = etNow(), et = etParts(now), t = et.h*60 + et.m;
  const weekend = et.wd==="Sat" || et.wd==="Sun";
  const nc = MKT.next_close ? Date.parse(MKT.next_close) : null;
  const no = MKT.next_open ? Date.parse(MKT.next_open) : null;
  if(MKT.is_open && nc) return {dot:"ok", val:`Open · closes in ${dhm(nc-now)}`,
    detail:`Regular session · closes ${etWhen(MKT.next_close)}`};
  const toOpen = no ? no-now : null;
  if(!weekend && t>=240 && t<570 && toOpen!=null && toOpen>0 && toOpen<6*3600e3)
    return {dot:"warn", val:`Pre-market · opens in ${dhm(toOpen)}`, detail:`Regular session opens ${etWhen(MKT.next_open)}`};
  if(!weekend && t>=960 && t<1200)
    return {dot:"warn", val:"After-hours", detail: no?`Next open ${etWhen(MKT.next_open)}`:"regular session closed"};
  return {dot:"mut", val: toOpen!=null?`Closed · opens in ${dhm(toOpen)}`:"Closed",
          detail: no?`Opens ${etWhen(MKT.next_open)}`:""};
}
// Coarse session phase for labeling live prices: 'rth' | 'pre' | 'post' | 'closed'. Alpaca's
// is_open is authoritative for RTH (it handles holidays); the ET bands split pre/after otherwise.
function mktSession(){
  if(!MKT) return 'closed';
  if(MKT.is_open) return 'rth';
  const et=etParts(etNow()), t=et.h*60+et.m;
  if(et.wd==="Sat" || et.wd==="Sun") return 'closed';
  if(t>=240 && t<570) return 'pre';       // 04:00–09:30 ET
  if(t>=960 && t<1200) return 'post';     // 16:00–20:00 ET
  return 'closed';
}
function sessionLabel(){ const s=mktSession(); return s==='pre'?'Pre-market':s==='post'?'After hours':''; }
function sessionChipInner(){ const s=sessionInfo();
  return `<span class="hdot ${s.dot}"></span><span class="sbk">Market</span><span class="sbv">${s.val}</span>`; }
function tickSession(){
  const et = etParts(etNow());
  const ec = $("etclock"); if(ec) ec.innerHTML = `<b>${String(et.h).padStart(2,"0")}:${String(et.m).padStart(2,"0")}:${String(et.s).padStart(2,"0")}</b> ET`;
  const sm = $("sbmarket"); if(sm){ const s=sessionInfo(); sm.innerHTML = sessionChipInner();
    sm.setAttribute("data-tip", `US market session (times in New York time).\n${s.detail||""}`); }
}
function htile(k, dot, val, sub){
  return `<div class="htile"><div class="hk">${k}</div><div class="hv">${dot?`<span class="hdot ${dot}"></span>`:""}${val}</div><div class="hs">${sub||""}</div></div>`;
}
function sbChip(dot, label, val, tip){
  return `<span class="sbchip"${tip?` data-tip="${esc(tip)}"`:""}><span class="hdot ${dot}"></span><span class="sbk">${label}</span><span class="sbv">${val}</span></span>`;
}
function renderHealth(h){
  if(!h) return;
  $("statusbar").style.display = "flex";
  const e=h.engine||{}, m=h.market, lr=h.last_rebalance, nr=h.next_rebalance||{}, dr=h.drift||{}, a=h.alerts_24h||{};
  const gate = lr ? lr.gate_passed : null;
  // Live session timer: cache the Alpaca clock (anchored to server time), then tick it every second.
  MKT = m ? {is_open:!!m.is_open, next_open:m.next_open, next_close:m.next_close,
             session_today: m.session_today,
             serverMs: m.timestamp?Date.parse(m.timestamp):Date.now(), fetchedAt: Date.now()} : null;
  // --- condensed global status bar (the "big important" signals, pinned on every tab) ---
  $("sbchips").innerHTML = [
    sbChip({live:"ok",stale:"warn",down:"bad"}[e.status]||"bad", "Engine",
           {live:"Live",stale:"Stale",down:"Down"}[e.status]||"Down",
           "Monitor heartbeat from the latest snapshot. Live = <3 min old, Stale = 3–10 min, Down = older / no data."),
    `<span class="sbchip" id="sbmarket"></span>`,   // live session timer, filled by tickSession()
    sbChip("ok", "Next rebalance",
           `${mdy(nr.date)}${nr.days_until==null?"":nr.days_until<=0?" · today":" · "+nr.days_until+"d"}`,
           "Next monthly rebalance (first trading day of the month). 'confirmed' = matched to the live NYSE calendar."),
    sbChip(gate==null?"mut":gate?"ok":"bad", "Risk gate", gate==null?"—":gate?"Pass":"Blocked",
           "Result of the last rebalance's pre-trade risk checks (leverage, single-name caps, covered-call coverage)."),
    sbChip(dr.l1==null?"mut":dr.l1>0.10?"warn":"ok", "Drift", dr.l1==null?"—":pct(dr.l1),
           "L1 distance between the live book and the last target weights. Telemetry only — drift never triggers a trade."),
    sbChip({ok:"ok",warn:"warn",error:"bad"}[a.worst]||"ok", "Alerts",
           a.latest ? (a.latest.message.length>46 ? a.latest.message.slice(0,45)+"…" : a.latest.message)
                    : "clear",
           (a.latest ? `Latest (${a.latest.severity}) · ${String(a.latest.ts).replace("T"," ").slice(0,19)} UTC:\n${a.latest.message}\n\n` : "")
             + `${a.total||0} alert(s) in last 24h · dot = worst severity`),
  ].join("");
  // --- full detail tiles (hidden behind the Details toggle) ---
  $("healthtiles").innerHTML = [
    htile("Engine", {live:"ok",stale:"warn",down:"bad"}[e.status]||"bad",
      {live:"Live",stale:"Stale",down:"Down"}[e.status]||"Down",
      e.age_s==null?"no snapshots yet":`monitor · data ${humanAge(e.age_s)} old`),
    m ? htile("Market", m.is_open?"ok":"mut", m.is_open?"Open":"Closed",
          m.is_open ? (m.next_close?`closes ${clockHM(m.next_close)}`:"")
                    : (m.next_open?`opens ${WD[new Date(m.next_open).getDay()]} ${clockHM(m.next_open)}`:""))
      : htile("Market","mut","—","live clock offline"),
    lr ? htile("Last rebalance", lr.gate_passed?"ok":"bad", `${mdy(lr.date)} · ${String(lr.trigger||"—").replace(/_/g," ")}`,
          lr.gate_passed?"risk gate ✓ passed":`gate ✗ ${lr.gate_reason||"blocked"}`)
      : htile("Last rebalance","mut","none yet","first run pending"),
    htile("Next rebalance","ok", mdy(nr.date),
      `${nr.days_until==null?"":nr.days_until<=0?"today":"in "+nr.days_until+" day"+(nr.days_until===1?"":"s")} · ${nr.source||""}`),
    dr.l1==null ? htile("Drift vs target","mut","–","no target book yet")
      : htile("Drift vs target", dr.l1>0.10?"warn":"ok", pct(dr.l1)+" L1",
          dr.n_drifting?`${dr.n_drifting} name${dr.n_drifting===1?"":"s"} off-target`:"on target"),
    htile("Alerts (24h)", {ok:"ok",warn:"warn",error:"bad"}[a.worst]||"ok",
      a.errors?`${a.errors} error${a.errors===1?"":"s"}`:a.warnings?`${a.warnings} warning${a.warnings===1?"":"s"}`:"all clear",
      a.latest ? a.latest.message : (a.total?`${a.total} in last 24h`:"none in 24h")),
  ].join("");
  const fr=h.freshness||{}, bits=[];
  if(e.snapshot_ts) bits.push(`snapshot ${String(e.snapshot_ts).replace("T"," ").slice(0,19)} UTC`);
  if(fr.factors_date) bits.push(`factors ${mdy(fr.factors_date)}`);
  if(fr.last_order_ts) bits.push(`last order ${ageOf(fr.last_order_ts)}`);
  $("healthsub").textContent = bits.join(" · ");
  tickSession();          // fill the live session chip immediately (then the 1s tick keeps it live)
}

// ---- KPI strip ------------------------------------------------------------
function kpis(s){
  const lev = s.leverage, cap = META.leverage_cap||2.0, over = lev!=null && lev>cap+1e-9;
  const cards = [];
  cards.push(kpi("Net asset value", usd2(s.nav),
    s.day_pnl_pct==null ? "" : `<span class="${sgn(s.day_pnl_pct)}">${spct(s.day_pnl_pct)} today</span>`,
    "Total account equity (cash + positions), live from Alpaca — exact to the cent."));
  cards.push(kpi("Day P&amp;L",
    `<span class="${sgn(s.day_pnl)}">${susd(s.day_pnl)}</span>`,
    s.day_pnl_pct==null ? "" : `<span class="${sgn(s.day_pnl_pct)}">${spct(s.day_pnl_pct)} vs prior</span>`,
    "Change in equity vs the prior trading day's close."));
  cards.push(`<div class="se-kpi kpi-lev" data-tip="Gross equity exposure ÷ account equity. The book runs ~2× on paper; the risk gate hard-caps it."><div class="label">Leverage</div>
      <div class="val ${over?'neg':''}">${lev==null?"–":lev.toFixed(2)+"×"}</div>
      <div class="gauge ${over?'over':''}"><i style="width:${lev==null?0:Math.min(100,lev/cap*100).toFixed(0)}%"></i></div>
      <div class="sub">cap ${cap.toFixed(1)}× · gross ${usd(s.gross_exposure)}</div></div>`);
  cards.push(kpi("Cash", usd2(s.cash),
    (s.cash!=null && s.nav) ? `${pct(s.cash/s.nav)} of NAV` : "",
    "Uninvested cash balance — exact to the cent."));
  cards.push(kpi("Premium collected", usd(s.premium_collected),
    (s.premium_collected && s.nav) ? `${pct(s.premium_collected/s.nav)} of NAV` : "lifetime",
    "Lifetime option premium collected — net credit from the SPY beta-overwrite spread (and any legacy covered calls)."));
  cards.push(kpi("Positions", fmt(s.n_positions),
    `drift (L1) <span class="${s.drift>0.1?'warn':'mut'}">${s.drift==null?"–":pct(s.drift)}</span>`,
    "Number of equity names held (options excluded)."));
  $("kpis").innerHTML = cards.join("");
}
// Performance KPI strip on the Overview (design language: Return · Sharpe · Max-DD · Volatility).
// Data already in /api/track_record (series_stats). '*' until ≥10 days of history.
function perfStrip(tr){
  if(!tr || !tr.available){ $("perfkpis").innerHTML = ""; return; }
  const star = tr.mature ? "" : "*";
  $("perfkpis").innerHTML = [
    kpi("Return (ITD)", `<span class="${sgn(tr.total_return)}">${spct(tr.total_return)}</span>`,
        "since inception, net", "Cumulative return of the paper book since go-live."),
    kpi("Sharpe"+star, tr.mature&&tr.sharpe!=null?tr.sharpe.toFixed(2):"—",
        tr.mature?"annualized":"needs ≥10 days", "Annualized return ÷ annualized volatility (rf ≈ 0)."),
    kpi("Max drawdown", `<span class="${sgn(tr.max_drawdown)}">${pct(tr.max_drawdown)}</span>`,
        "worst peak-to-trough", "Largest peak-to-trough NAV decline since inception."),
    kpi("Volatility"+star, tr.mature&&tr.ann_vol!=null?pct(tr.ann_vol):"—",
        tr.mature?"annualized":"needs ≥10 days", "Annualized standard deviation of daily returns."),
  ].join("");
}
function kpi(label, val, sub, tip){
  return `<div class="se-kpi"${tip?` data-tip="${esc(tip)}"`:""}><div class="label">${label}</div><div class="val">${val}</div>${sub?`<div class="sub">${sub}</div>`:""}</div>`;
}

// ---- NAV sparkline (SVG) --------------------------------------------------
function sparkline(hist){
  const host = $("navspark");
  if(!hist || hist.length < 2){ host.innerHTML = '<div class="se-empty">not enough history yet — one point so far</div>'; $("sparkmeta").innerHTML=""; $("navrange").textContent=""; return; }
  const W=1200, H=230, pl=58, pr=14, pt=12, pb=24, pw=W-pl-pr, ph=H-pt-pb;
  const navs = hist.map(p=>p.nav).filter(v=>v!=null);
  let lo=Math.min(...navs), hi=Math.max(...navs); if(lo===hi){hi+=1;lo-=1;} const pad=(hi-lo)*0.10; lo-=pad; hi+=pad;
  const n=hist.length;
  const X=i=> pl + (n<=1?0:i/(n-1)*pw);
  const Y=v=> pt + (hi-v)/(hi-lo)*ph;
  const grid=cssv('--grid','#16223a'), mut=cssv('--muted','#65758c');
  const last=hist[hist.length-1].nav, first=hist[0].nav;
  const col = cssv('--accent','#46b8ad');   // equity curve is the primary series → teal (SFI)
  const ML="font-family:var(--mono)";
  const kfmt = v => "$"+Math.round(v/1000)+"k";
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="NAV over time" style="display:block">`;
  for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4, y=Y(v).toFixed(1);
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/>`+
       `<text x="${pl-8}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${mut}" style="${ML}">${kfmt(v)}</text>`; }
  const step=Math.max(1,Math.round(n/7));
  for(let i=0;i<n;i+=step){ s+=`<text x="${X(i).toFixed(1)}" y="${H-7}" text-anchor="middle" font-size="11" fill="${mut}" style="${ML}">${(hist[i].ts||"").slice(5,10)}</text>`; }
  const d = hist.map((p,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(p.nav).toFixed(1)).join(" ");
  const y0=(H-pb).toFixed(1), area=`${d} L${X(n-1).toFixed(1)} ${y0} L${X(0).toFixed(1)} ${y0} Z`;
  s+=`<path d="${area}" fill="${col}" fill-opacity="0.05"/>`;
  s+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/>`;
  s+=`<circle cx="${X(n-1).toFixed(1)}" cy="${Y(last).toFixed(1)}" r="2.6" fill="${col}"/>`;
  host.innerHTML = s+"</svg>";
  const chg=last-first, chgp=first? chg/first : null;
  $("sparkmeta").innerHTML =
    `<div class="m"><div class="l">Current</div><div class="v">${usd(last)}</div></div>
     <div class="m"><div class="l">Change (window)</div><div class="v ${sgn(chg)}">${chg>=0?"+":""}${usd(chg).replace("-","")} <span style="font-size:12px">(${spct(chgp)})</span></div></div>
     <div class="m"><div class="l">Window high / low</div><div class="v">${usd(Math.max(...navs))} <span class="mut" style="font-size:12px">/ ${usd(Math.min(...navs))}</span></div></div>`;
  const t0=(hist[0].ts||"").slice(0,10), t1=(hist[hist.length-1].ts||"").slice(0,10);
  $("navrange").textContent = `${hist.length} snapshots · ${t0} → ${t1}`;
}

// ---- live price buffers (client-side, fed by the 1s /api/state tick) ------
// _px: rolling per-symbol live price path for the row sparklines; _navBase: the fetched NAV
// history for the Overview chart, with a live tail point appended each tick so the tip tracks
// live NAV (fixes the "numbers move but the chart is frozen" confusion). _calls: cached book.
const _px = {};
let _navBase = [];
let _calls = [];
let _overlay = null;                 // SPY beta-overwrite spread state (/api/overlay)
const PX_KEEP = 140;

function livePx(r){ return r.last_price != null ? r.last_price : (r.qty ? r.market_value / r.qty : null); }

function pushPx(s){
  (s.positions || []).forEach(r => {
    const p = livePx(r); if (p == null) return;
    const buf = (_px[r.symbol] = _px[r.symbol] || []);
    if (buf[buf.length - 1] !== p) buf.push(p);
    if (buf.length > PX_KEEP) buf.shift();
  });
}

// Seed each row's sparkline with a real intraday price window (Alpaca hourly bars) so it shows a
// meaningful path immediately, instead of growing one point per live tick on the sparse IEX feed
// (the "dead / 2-3 points" problem). Called once on boot; live ticks then extend the tail.
async function seedSparklines(){
  const h = await get("/api/price_history");
  if (!h || !h.history) return;
  for (const [sym, arr] of Object.entries(h.history)) {
    if (Array.isArray(arr) && arr.length >= 2) _px[sym] = arr.slice(-PX_KEEP);
  }
}

// A tiny inline sparkline (teal series — green/red are reserved for signed P&L per the design language).
function miniSpark(arr){
  if (!arr || arr.length < 2) return '<span class="mut">–</span>';
  const w = 66, h = 18, lo = Math.min(...arr), hi = Math.max(...arr), rng = (hi - lo) || 1;
  const X = i => (i / (arr.length - 1)) * (w - 2) + 1, Y = v => h - 1 - ((v - lo) / rng) * (h - 2);
  const d = arr.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block" aria-hidden="true">`
    + `<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
}

// ---- ticker tape (pinned, continuously-scrolling live prices) --------------
// The DOM is rebuilt only when the held set changes (rare — monthly); every 1s tick just updates
// the numbers/sparkline in place, so the marquee animation never restarts.
let _tapeSyms = "";
function tapeItem(sym){
  return `<span class="tape-item" data-sym="${sym}"><span class="tape-dot"></span><span class="tape-tkr">${sym}</span>`
    + `<span class="tape-px"></span><span class="tape-spk"></span><span class="tape-chg"></span><span class="tape-call"></span></span>`;
}
function renderTape(s){
  const rows = (s.positions||[]).filter(r => r.symbol && !_isOpt(r.symbol) && r.qty);
  if(!rows.length){ $("tape").style.display="none"; return; }
  $("tape").style.display="flex";
  // Global session badge: when the board is priced off pre/after-hours prints (not RTH), say so —
  // the day % on each item is then the extended-hours move vs the prior close, not the RTH change.
  const sess = mktSession(), badge = $("tapeSession");
  if(badge){ if(sess==='pre'||sess==='post'){ badge.style.display="inline-flex";
      badge.innerHTML = `<i></i>${sess==='pre'?'Pre-market':'After hours'}`; }
    else badge.style.display="none"; }
  const key = rows.map(r=>r.symbol).join(",");
  if(key !== _tapeSyms){                                  // holdings changed → rebuild the marquee
    _tapeSyms = key;
    const items = rows.map(r=>tapeItem(r.symbol)).join("");
    const track = $("tapetrack");
    track.innerHTML = items + items;                      // two copies → seamless −50% loop
    track.style.animationDuration = Math.max(34, rows.length * 3.4) + "s";
  }
  const covered = new Set((_calls||[]).map(c=>c.underlying));
  const green=cssv('--green','#5fb088'), red=cssv('--red','#cf6f66'), mut=cssv('--muted','#65758c');
  rows.forEach(r => {
    const p = livePx(r), chg = r.day_pct, c = chg==null?mut:chg>=0?green:red;
    const spk = sparkP((_px[r.symbol]||[]).slice(-12), 34, 13, 1.5);
    document.querySelectorAll('.tape-item[data-sym="'+r.symbol+'"]').forEach(el => {
      el.querySelector('.tape-dot').style.background = secColor((REF[r.symbol.toUpperCase()]||{}).sector);
      el.querySelector('.tape-px').textContent = p==null?"–":p.toFixed(2);
      el.querySelector('.tape-spk').innerHTML = spk
        ? `<svg width="34" height="13" viewBox="0 0 34 13" preserveAspectRatio="none" style="display:block"><path d="${spk}" fill="none" stroke="${c}" stroke-width="1.3" stroke-linejoin="round"/></svg>` : "";
      const chgEl = el.querySelector('.tape-chg'); chgEl.textContent = chg==null?"":spct(chg,2); chgEl.style.color = c;
      el.querySelector('.tape-call').textContent = covered.has(r.symbol) ? "CALL" : "";
    });
  });
}

// Redraw the Overview NAV chart with a live tail so its tip tracks the live NAV every second.
function drawNav(liveNav){
  const tail = liveNav != null ? [{ ts: new Date().toISOString(), nav: liveNav }] : [];
  sparkline(_navBase.concat(tail));
}

// ---- covered-call helpers -------------------------------------------------
function dte(exp){ if(!exp) return null; const d=Math.round((new Date(exp+"T00:00:00")-Date.now())/864e5); return d; }

// ---- loaders + shared freshness bookkeeping -------------------------------
let snapTs = null, lastFetch = 0, backendDown = false, portfolioFirst = true;
function noteFresh(s){
  backendDown = !s;
  if(s) snapTs = s.ts ? new Date(s.ts.replace(" ","T")+(/[zZ]|\+/.test(s.ts)?"":"Z")).getTime() : null;
  lastFetch = Date.now();
  tickFreshness();
}

// ===================== Overview — design-doc layout =====================
// Ported from design_lang/SFI Dashboard mock: Trading session · NAV hero (sign-flash) · 6-col KPI
// ribbon · Holdings treemap · Covered-call gauge · Events · P&L waterfall · System health.
function sparkP(arr,w,h,p){ p=p||1.5; const a=(arr||[]).filter(v=>v!=null); if(a.length<2) return "";
  let lo=Math.min(...a),hi=Math.max(...a); if(hi-lo<1e-9) hi=lo+1;
  return a.map((v,i)=>(i?"L":"M")+(p+i/(a.length-1)*(w-2*p)).toFixed(1)+" "+(p+(hi-v)/(hi-lo)*(h-2*p)).toFixed(1)).join(" "); }
function hms(secs){ const s=Math.max(0,Math.floor(secs)),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),
  m=Math.floor(s%3600/60),ss=s%60,p=n=>String(n).padStart(2,"0"); return (d>0?d+"d ":"")+p(h)+":"+p(m)+":"+p(ss); }
function ovPanel(title,sub,body,pad,src,fill){ return `<div class="ovpanel"${fill?' style="display:flex;flex-direction:column;height:100%"':''}><div class="ovhead"><span class="ovhk">${title}</span>`
  +`<span class="ovhs">${sub}${src?srcInfo(src):''}</span></div><div style="padding:${pad||'14px 16px'}${fill?';flex:1;display:flex;flex-direction:column;justify-content:center':''}">${body}</div></div>`; }

function renderSession(){
  const host=$("ov-session"); if(!host) return;
  if(!MKT){ host.innerHTML=ovPanel("Trading session","NYSE","<div style='font:400 12px var(--mono);color:var(--muted)'>market clock offline</div>","14px 16px","clock"); return; }
  const now=etNow(), et=etParts(now), secs=et.h*3600+et.m*60+et.s, open=9.5*3600;
  const nc=MKT.next_close?Date.parse(MKT.next_close):null, no=MKT.next_open?Date.parse(MKT.next_open):null;
  // Close bound from the LIVE clock while the session runs — half-days close 13:00, and a
  // hardcoded 16:00 would show ~50% progress after the market shut (see the holiday fix's sibling).
  const cET=(MKT.is_open&&nc)?etParts(nc):null, close=cET?(cET.h*3600+cET.m*60):16*3600;
  const hhmm=s=>`${String(Math.floor(s/3600)).padStart(2,'0')}:${String(Math.floor(s%3600/60)).padStart(2,'0')}`;
  const f=Math.max(0,Math.min(1,(secs-open)/(close-open)));
  const clk=`<span style="font:500 22px var(--mono);color:var(--fg);font-variant-numeric:tabular-nums">${String(et.h).padStart(2,'0')}:${String(et.m).padStart(2,'0')}:${String(et.s).padStart(2,'0')}</span><span style="font:400 11px var(--mono);color:var(--muted)">ET</span>`;
  let sub, pill, pillCol, barW, noSession=false;
  if(MKT.is_open && nc){ sub=`· ${(f*100).toFixed(0)}% through session · ${hms((nc-now)/1000)} to close`; pill="Market open"; pillCol="#5fb088"; barW=f*100; }
  else if(MKT.session_today===false){                 // NYSE holiday / weekend: NO session today
    const si=sessionInfo(); noSession=true; barW=0;
    const why = (et.wd==="Sat"||et.wd==="Sun") ? "weekend" : "market holiday";
    sub=`· no session today — ${why} · ${si.val.replace(/^Closed · /,"")}`;
    pill="Market closed"; pillCol="#65758c";
  }
  else { const si=sessionInfo(); sub=`· ${si.val}`; const warn=si.dot==='warn'; pill=warn?"Extended hours":"Market closed"; pillCol=warn?"#d8a84b":"#65758c"; barW=secs>=close?100:secs<open?0:f*100; }
  const head=`<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px"><div style="display:flex;align-items:baseline;gap:9px">${clk}<span style="font:400 11px var(--mono);color:var(--muted)">${sub}</span></div><span style="display:inline-flex;align-items:center;gap:6px;font:500 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:${pillCol}"><span style="width:7px;height:7px;border-radius:50%;background:${pillCol}"></span>${pill}</span></div>`;
  const bar = noSession
    ? `<div style="position:relative;height:9px;border-radius:5px;background:var(--panel-2);margin-top:11px;background-image:repeating-linear-gradient(45deg,transparent,transparent 5px,#16223a 5px,#16223a 10px)"></div>`
    : `<div style="position:relative;height:9px;border-radius:5px;background:var(--panel-2);overflow:hidden;margin-top:11px"><div style="position:absolute;left:0;top:0;height:100%;width:${barW.toFixed(1)}%;background:linear-gradient(90deg,#2a6f63,#46b8ad)"></div><div style="position:absolute;top:-3px;width:2px;height:15px;background:var(--fg);left:${barW.toFixed(1)}%"></div></div>`;
  const marks = noSession
    ? `<div style="display:flex;justify-content:center;margin-top:6px;font:400 10px var(--mono);color:var(--muted)"><span>no NYSE session today${MKT.next_open?` · next open ${etWhen(MKT.next_open)}`:""}</span></div>`
    : `<div style="display:flex;justify-content:space-between;margin-top:6px;font:400 10px var(--mono);color:var(--muted)"><span>09:30 open</span><span>${hhmm((open+close)/2)}</span><span>${hhmm(close)} close</span></div>`;
  host.innerHTML=ovPanel("Trading session","NYSE · regular hours", head+bar+marks, "14px 16px", "clock");
}

function navAreaSVG(hist){
  const navs=hist.map(p=>p.nav).filter(v=>v!=null);
  if(navs.length<2) return '<div class="se-empty">not enough history yet — one point so far</div>';
  const W=1200,H=230,pl=58,pr=14,pt=12,pb=24,pw=W-pl-pr,ph=H-pt-pb,n=hist.length;
  let lo=Math.min(...navs),hi=Math.max(...navs); if(lo===hi){hi+=1;lo-=1;} const pad=(hi-lo)*0.10; lo-=pad; hi+=pad;
  const X=i=>pl+(n<=1?0:i/(n-1)*pw), Y=v=>pt+(hi-v)/(hi-lo)*ph;
  const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),col=cssv('--accent','#46b8ad'),ML="font-family:var(--mono)";
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="display:block">`;
  for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4,y=Y(v).toFixed(1);
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="${grid}"/><text x="${pl-8}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${mut}" style="${ML}">$${Math.round(v/1000)}k</text>`; }
  const step=Math.max(1,Math.round(n/7));
  for(let i=0;i<n;i+=step) s+=`<text x="${X(i).toFixed(1)}" y="${H-7}" text-anchor="middle" font-size="11" fill="${mut}" style="${ML}">${(hist[i].ts||"").slice(5,10)}</text>`;
  const d=hist.map((p,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(p.nav).toFixed(1)).join(" "),y0=(H-pb).toFixed(1);
  s+=`<path d="${d} L${X(n-1).toFixed(1)} ${y0} L${X(0).toFixed(1)} ${y0} Z" fill="${col}" fill-opacity="0.05"/>`;
  s+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/>`;
  s+=`<circle cx="${X(n-1).toFixed(1)}" cy="${Y(navs[navs.length-1]).toFixed(1)}" r="2.6" fill="${col}"/>`;
  return s+"</svg>";
}
let _lastNav=null, _navWin=localStorage.getItem('sepi_navwin')||'ALL', _ovS=null, _attr=null;
const NAV_WINS=[["1W","1W"],["1M","1M"],["3M","3M"],["YTD","YTD"],["ALL","All"]];
function navWindowed(){
  if(_navWin==='ALL'||_navBase.length<2) return _navBase;
  let cut;
  if(_navWin==='YTD') cut=new Date(new Date().getFullYear(),0,1).getTime();
  else cut=Date.now()-({ '1W':7,'1M':30,'3M':90 }[_navWin])*864e5;
  const f=_navBase.filter(p=>{ const t=tsMs(p.ts); return t==null||t>=cut; });
  return f.length>=2?f:_navBase.slice(-2);
}
function setNavWin(w){ _navWin=w; localStorage.setItem('sepi_navwin', w); if(_ovS) renderNavHero(_ovS); }
function renderNavHero(s){
  const host=$("ov-navhero"); if(!host||s.nav==null) return;
  const nav=s.nav, dp=s.day_pnl, dpp=s.day_pnl_pct;
  const dayColor=dp==null?"var(--muted)":dp>0?"var(--green)":dp<0?"var(--red)":"var(--muted)";
  // Outside RTH the day P&L is driven by pre/after-hours prints, not the RTH session — label it.
  const sess=mktSession(), sessTag=sess==='pre'?'Pre-market':sess==='post'?'After hours':'';
  const dayWord=sess==='pre'?'pre-market':sess==='post'?'after hours':'today';
  const daySub=(dp==null?"–":(dp>=0?"+$":"-$")+fmt(Math.abs(dp),0))+(dpp==null?"":" · "+spct(dpp)+" "+dayWord);
  const base=navWindowed();
  const hist=base.concat([{ts:new Date().toISOString(),nav}]);
  const navs=hist.map(p=>p.nav).filter(v=>v!=null);
  const winChg=navs.length?nav-navs[0]:null, winPct=(navs.length&&navs[0])?winChg/navs[0]:null;
  const smallCol=navs.length&&nav>=navs[0]?"#5fb088":"#cf6f66";
  host.innerHTML=`<div class="ovpanel">
    <div style="display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:18px;padding:18px 22px 14px;border-bottom:1px solid var(--line-soft)">
      <div>
        <div style="font:600 10px 'Space Grotesk';letter-spacing:.16em;text-transform:uppercase;color:var(--muted)">Net asset value${srcInfo('live')}</div>
        <div style="display:flex;align-items:baseline;gap:14px;margin-top:8px">
          <div id="navbig" class="ovflash" style="font:500 46px/1 'Space Grotesk';letter-spacing:-.01em;color:var(--fg);font-variant-numeric:tabular-nums;padding:1px 6px;margin:-1px -6px">${usd(nav)}</div>
          <div id="navpnl" class="ovflash" style="font:500 14px var(--mono);color:${dayColor};padding:1px 5px;margin:-1px -5px">${daySub}</div>
          ${sessTag?`<span style="font:600 10px 'Space Grotesk';letter-spacing:.07em;text-transform:uppercase;color:var(--amber);background:rgba(216,168,75,.10);border:1px solid rgba(216,168,75,.28);border-radius:4px;padding:2px 6px;align-self:center">${sessTag}</span>`:''}
        </div>
        <div style="margin-top:11px;display:flex;align-items:center;gap:10px"><svg width="240" height="38" viewBox="0 0 240 38" preserveAspectRatio="none" style="display:block"><path d="${sparkP(navs.slice(-48),240,38,2)}" fill="none" stroke="${smallCol}" stroke-width="1.6" stroke-linejoin="round"/></svg><span style="font:400 10px var(--mono);color:var(--muted)">intraday · live</span></div>
      </div>
      <div style="display:flex;gap:34px;padding-bottom:4px">
        <div><div style="font:500 10px 'Space Grotesk';letter-spacing:.12em;text-transform:uppercase;color:var(--muted)">Change (window)</div><div style="font:500 16px var(--mono);color:${winChg==null?'var(--fg)':winChg>=0?'var(--green)':'var(--red)'};margin-top:5px">${winChg==null?'–':(winChg>=0?'+':'−')+usd(Math.abs(winChg))+' ('+spct(winPct)+')'}</div></div>
        <div><div style="font:500 10px 'Space Grotesk';letter-spacing:.12em;text-transform:uppercase;color:var(--muted)">Window high / low</div><div style="font:500 16px var(--mono);color:var(--fg);margin-top:5px">${navs.length?usd(Math.max(...navs)):'–'} <span style="color:var(--muted)">/ ${navs.length?usd(Math.min(...navs)):'–'}</span></div></div>
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:4px;padding:8px 18px 0">${NAV_WINS.map(([k,l])=>`<button onclick="setNavWin('${k}')" class="evtf${_navWin===k?' on':''}">${l}</button>`).join('')}</div>
    <div style="padding:6px 8px 4px">${navAreaSVG(hist)}</div>
    <div style="padding:4px 22px 12px;font:400 10.5px var(--mono);color:var(--muted)">${base.length} snapshots · ${(base[0]?.ts||'').slice(0,10)} → ${(base[base.length-1]?.ts||'').slice(0,10)}</div>
  </div>`;
  if(_lastNav!=null && nav!==_lastNav){ const dir=nav>_lastNav?1:-1;
    ["navbig","navpnl"].forEach(id=>{ const el=$(id); if(el&&el.animate)
      el.animate([{backgroundColor:dir>0?'rgba(95,176,136,.30)':'rgba(207,111,102,.30)'},{backgroundColor:'rgba(0,0,0,0)'}],{duration:680,easing:'ease-out'}); }); }
  _lastNav=nav;
}

function kpiCell(label,val,sub,opts){ opts=opts||{};
  return `<div style="background:var(--panel);padding:13px 16px"><div style="font:600 10px 'Space Grotesk';letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">${label}</div>`
    +`<div style="font:500 22px var(--mono);margin-top:8px;color:${opts.valColor||'var(--fg)'}">${val}</div>${opts.extra||''}`
    +`<div style="font:400 10.5px var(--mono);margin-top:5px;color:${opts.subColor||'var(--muted)'}">${sub||''}</div></div>`;
}
function renderKpiRibbon(s){
  const host=$("ov-kpi"); if(!host) return;
  const dp=s.day_pnl, dpp=s.day_pnl_pct, dayCol=dp==null?'var(--muted)':dp>0?'var(--green)':dp<0?'var(--red)':'var(--muted)';
  const dpStr=(dp==null?'–':(dp>=0?'+$':'-$')+fmt(Math.abs(dp),0));
  const daySub=dpStr+(dpp==null?'':' · '+spct(dpp)+' today');
  const cap=META.leverage_cap||2.0, lev=s.leverage, levW=lev==null?0:Math.min(100,lev/cap*100);
  const levBar=`<div style="height:4px;border-radius:2px;background:var(--panel-2);margin-top:8px;overflow:hidden"><div style="height:100%;background:var(--accent);width:${levW.toFixed(0)}%"></div></div>`;
  host.innerHTML=ribWrap(`<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden">`
    +kpiCell("Net asset value",usd(s.nav),daySub,{subColor:dayCol})
    +kpiCell("Day P&L",dpStr,(dpp==null?'':spct(dpp)+' vs prior close'),{valColor:dayCol})
    +kpiCell("Leverage",lev==null?'–':lev.toFixed(2)+'×',`cap ${cap.toFixed(1)}× · gross ${usd(s.gross_exposure)}`,{extra:levBar})
    +kpiCell("Cash",usd(s.cash),(s.nav?`${(s.cash/s.nav*100).toFixed(1)}% of NAV`:''))
    +kpiCell("Premium collected",usd(s.premium_collected),(s.nav&&s.premium_collected?`${(s.premium_collected/s.nav*100).toFixed(1)}% of NAV · lifetime`:'SPY overlay'))
    +kpiCell("Positions",fmt(s.n_positions),`drift ${s.drift!=null?pct(s.drift):'–'} L1`)
    +`</div>`, "kpiov");
}

function renderTreemap(s){
  const host=$("ov-treemap"); if(!host) return;
  const items=(s.positions||[]).filter(p=>p.symbol&&!_isOpt(p.symbol)&&p.weight)
    .map(p=>({sym:p.symbol,value:p.weight,chg:p.day_pct==null?0:p.day_pct})).sort((a,b)=>b.value-a.value);
  if(!items.length){ host.innerHTML='<div class="se-empty">no holdings yet</div>'; return; }
  // Render at the container's actual pixel size (minus the 9px inset the SVG sits in) so the
  // viewBox is 1:1 — no stretched text. The SVG is absolutely positioned, so it can't feed its
  // height back into the container (which previously made the panel slowly grow every tick).
  const W=Math.max(320,Math.round((host.clientWidth||1021)-18)), H=Math.max(240,Math.round((host.clientHeight||470)-18));
  const tm=(arr,x,y,w,h,horiz)=>{ if(arr.length===1){ arr[0].rect={x,y,w,h}; return; }
    const tot=arr.reduce((a,b)=>a+b.value,0),half=tot/2; let c=0,best=0,bd=1e9;
    for(let k=0;k<arr.length-1;k++){ c+=arr[k].value; if(Math.abs(c-half)<bd){bd=Math.abs(c-half);best=k;} }
    const A=arr.slice(0,best+1),B=arr.slice(best+1),fa=A.reduce((a,b)=>a+b.value,0)/tot;
    if(horiz){ tm(A,x,y,w*fa,h,!horiz); tm(B,x+w*fa,y,w*(1-fa),h,!horiz); } else { tm(A,x,y,w,h*fa,!horiz); tm(B,x,y+h*fa,w,h*(1-fa),!horiz); } };
  tm(items,0,0,W,H,W>=H); let r="";
  items.forEach(it=>{ const t=Math.max(-1,Math.min(1,it.chg/0.03)),col=t>=0?"95,176,136":"207,111,102",
    al=(0.12+Math.abs(t)*0.5).toFixed(2),R=it.rect,big=R.w>70&&R.h>34;
    r+=`<rect x="${R.x.toFixed(1)}" y="${R.y.toFixed(1)}" width="${(R.w-1.4).toFixed(1)}" height="${(R.h-1.4).toFixed(1)}" fill="rgba(${col},${al})" stroke="#0a1322" stroke-width="1.4"/>`;
    r+=`<text x="${(R.x+8).toFixed(1)}" y="${(R.y+18).toFixed(1)}" font-size="14" font-family="IBM Plex Mono" font-weight="600" fill="#eaf2fb">${it.sym}</text>`;
    if(big) r+=`<text x="${(R.x+8).toFixed(1)}" y="${(R.y+32).toFixed(1)}" font-size="10.5" font-family="IBM Plex Mono" fill="rgba(234,242,251,.72)">${it.chg>=0?"+":""}${(it.chg*100).toFixed(2)}%</text>`; });
  host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%" style="display:block">${r}</svg>`;
}

// Short "Jul 31" expiry label from an ISO date (no year — the DTE carries the horizon).
const _MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function expShort(iso){ if(!iso) return "–"; const p=String(iso).split("-"); return p.length===3?`${_MON[(+p[1]||1)-1]} ${+p[2]}`:iso; }

// Overview overlay panel. The book runs a portfolio-level SPY beta-overwrite as a defined-risk
// vertical call spread (short target-delta / long wing, same expiry) sized to the book's market
// beta — the low-beta names' single-name options are too illiquid to write. The gauge arc reads
// β overwritten (short-leg notional ÷ gross equity); a low-beta book overwrites proportionally
// less. Falls back to the classic per-name covered-call gauge when overlay_mode = "per_name".
function renderGauge(s,calls,ov){
  const host=$("ov-gauge"); if(!host) return;
  if(ov && ov.mode==="per_name"){ renderCoveredGauge(host,s,calls); return; }
  const st=(l,v,c,sub)=>`<div style="flex:1;min-width:0"><div style="font:600 10px 'Space Grotesk';letter-spacing:.08em;text-transform:uppercase;color:var(--muted)">${l}</div>`
    +`<div style="font:500 16px var(--mono);color:${c||'var(--fg)'};margin-top:5px;white-space:nowrap">${v}</div>`
    +(sub?`<div style="font:400 10px var(--mono);color:var(--muted);margin-top:2px">${sub}</div>`:"")+`</div>`;
  if(!ov || !ov.active){
    const body=`<div style="padding:18px 16px;font:400 11.5px var(--mono);color:var(--muted);line-height:1.55">`
      +`No SPY spread open — the overlay writes one beta-sized SPY call spread (short ~0.30Δ / long wing) at each monthly rebalance, sized to the book's market beta.</div>`;
    host.innerHTML=ovPanel("SPY beta overlay","β-sized call spread", body, "0", "overlay"); return;
  }
  const bo=(ov.beta_overwritten!=null)?ov.beta_overwritten:null;   // short-leg notional ÷ gross ≈ coverage·β_p
  const frac=bo!=null?Math.max(0,Math.min(1,bo)):0;
  const W=230,H=116,cx=115,cy=108,rr=88,a0=Math.PI,a1=0,ang=a0+(a1-a0)*frac;
  const pt=an=>[cx+rr*Math.cos(an),cy-rr*Math.sin(an)];
  const arc=(an0,an1,c,wd)=>{ const [x0,y0]=pt(an0),[x1,y1]=pt(an1),large=Math.abs(an1-an0)>Math.PI?1:0,sw=an1<an0?1:0;
    return `<path d="M${x0.toFixed(1)} ${y0.toFixed(1)} A${rr} ${rr} 0 ${large} ${sw} ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none" stroke="${c}" stroke-width="${wd}" stroke-linecap="round"/>`; };
  const g=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block">${arc(a0,a1,"#16223a",11)}${frac>0.001?arc(a0,ang,"#46b8ad",11):""}`
    +`<text x="${cx}" y="${cy-28}" text-anchor="middle" font-size="26" fill="#eaf2fb" font-family="IBM Plex Mono" font-weight="500">${bo!=null?bo.toFixed(2):"–"}</text>`
    +`<text x="${cx}" y="${cy-11}" text-anchor="middle" font-size="10" fill="#65758c" font-family="Space Grotesk" letter-spacing="1.3">β OVERWRITTEN</text></svg>`;
  const d=dte(ov.expiration);
  // Assignment-risk readout: how far SPY sits below the short strike (the plateau edge).
  const dist=(ov.spot&&ov.short_strike)?(ov.short_strike/ov.spot-1):null;
  const dCol=dist==null?'var(--muted)':dist<=0?'var(--red)':dist<0.01?'var(--amber)':'#5fb088';
  const dState=dist==null?'':dist<=0?'in the money — assignment risk':dist<0.01?'near the strike — watch it':'upside runway before the plateau';
  const spread=(ov.short_strike!=null?fmt(ov.short_strike,0):"–")+" / "+(ov.long_strike!=null?fmt(ov.long_strike,0):"–");
  const totCredit=ov.premium_total!=null?usd(ov.premium_total):"–";
  const body=`<div style="display:flex;align-items:center;gap:10px"><div style="flex:none">${g}</div><div style="flex:1;display:flex;flex-direction:column;gap:12px;padding-left:4px">`
    +`<div style="display:flex;gap:10px">${st("Spread",spread+" C",null,`exp ${expShort(ov.expiration)} · ${d==null?"–":d+"d"}`)}${st("Net credit",ov.net_credit!=null?"$"+ov.net_credit.toFixed(2):"–","#5fb088",totCredit+" total")}</div>`
    +`<div style="display:flex;gap:10px">${st("Short Δ",ov.short_delta==null?"–":ov.short_delta.toFixed(2))}${st("Spreads",fmt(ov.contracts))}${st("Max risk",ov.max_risk!=null?usd(ov.max_risk):"–","#cf9a5f")}</div>`
    +(dist!=null?`<div style="display:flex;gap:10px">${st("Spot → short strike",(dist<=0?"ITM ":"+")+(Math.abs(dist)*100).toFixed(1)+"%",dCol,dState)}</div>`:"")
    +`</div></div>`;
  host.innerHTML=ovPanel("SPY beta overlay","β-sized call spread", body, "14px 16px", "overlay");
}

// Legacy per-name covered-call gauge (kept for overlay_mode = "per_name").
function renderCoveredGauge(host,s,calls){
  const px={}; (s.positions||[]).forEach(p=>{ px[p.symbol]=p.last_price!=null?p.last_price:(p.qty?p.market_value/p.qty:null); });
  const held={}; (s.positions||[]).forEach(p=>{ if(!_isOpt(p.symbol)) held[p.symbol]=p.qty||0; });
  const coverable=Object.keys(held).filter(k=>held[k]>=100);
  let elig=0; coverable.forEach(k=>elig+=held[k]*(px[k]||0));
  let cov$=0; (calls||[]).forEach(c=>{ const sh=Math.min((c.contracts||0)*100,held[c.underlying]||(c.contracts||0)*100); cov$+=sh*(px[c.underlying]||0); });
  const cov=elig?Math.min(1,cov$/elig):0;
  let nd=0,sumW=0,wById={}; (s.positions||[]).forEach(p=>{ if(_isOpt(p.symbol))return; wById[p.symbol]=p.weight||0; nd+=(p.weight||0); sumW+=(p.weight||0); });
  (calls||[]).forEach(c=>{ nd-=(wById[c.underlying]||0)*(c.delta==null?0.30:c.delta); });
  const netDelta=sumW?nd/sumW:0;
  let theta=0; (calls||[]).forEach(c=>{ const d=dte(c.expiration); if(c.premium&&d&&d>0) theta+=c.premium/d; });
  const yld=(s.nav&&theta)?theta*365/s.nav*100:null;
  const W=230,H=116,cx=115,cy=108,rr=88,a0=Math.PI,a1=0,ang=a0+(a1-a0)*cov;
  const pt=an=>[cx+rr*Math.cos(an),cy-rr*Math.sin(an)];
  const arc=(an0,an1,c,wd)=>{ const [x0,y0]=pt(an0),[x1,y1]=pt(an1),large=Math.abs(an1-an0)>Math.PI?1:0,sw=an1<an0?1:0;
    return `<path d="M${x0.toFixed(1)} ${y0.toFixed(1)} A${rr} ${rr} 0 ${large} ${sw} ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none" stroke="${c}" stroke-width="${wd}" stroke-linecap="round"/>`; };
  const g=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block">${arc(a0,a1,"#16223a",11)}${cov>0.001?arc(a0,ang,"#46b8ad",11):""}`
    +`<text x="${cx}" y="${cy-28}" text-anchor="middle" font-size="29" fill="#eaf2fb" font-family="IBM Plex Mono" font-weight="500">${(cov*100).toFixed(0)}%</text>`
    +`<text x="${cx}" y="${cy-11}" text-anchor="middle" font-size="10" fill="#65758c" font-family="Space Grotesk" letter-spacing="1.5">COVERED</text></svg>`;
  const st=(l,v,c)=>`<div style="flex:1"><div style="font:600 10px 'Space Grotesk';letter-spacing:.08em;text-transform:uppercase;color:var(--muted)">${l}</div><div style="font:500 16px var(--mono);color:${c||'var(--fg)'};margin-top:5px">${v}</div></div>`;
  const body=`<div style="display:flex;align-items:center;gap:10px"><div style="flex:none">${g}</div><div style="flex:1;display:flex;flex-direction:column;gap:12px;padding-left:4px">`
    +`<div style="display:flex;gap:10px">${st("Net delta",netDelta.toFixed(2))}${st("Theta / day",theta?"+"+usd(theta):"–","#5fb088")}</div>`
    +`<div style="display:flex;gap:10px">${st("Prem. yield",yld==null?"–":yld.toFixed(1)+"%","#5fb088")}${st("Calls open",fmt((calls||[]).length))}</div></div></div>`;
  host.innerHTML=ovPanel("Covered-call overlay","Δ · θ · coverage", body, "14px 16px", "calls");
}

let _eventsData=[];
const EVT_FILTERS=[["all","All"],["earnings","Earnings"],["expiry","Expiries"],["rebalance","Rebalance"]];
let _evtFilter="all";
function setEvtFilter(f){ _evtFilter=f; renderEventsPanel(); }
function evtSecsLeft(dateStr){ return (Date.parse(dateStr+"T00:00:00-04:00")-etNow())/1000; }
function renderEventsPanel(){
  const host=$("ov-events"); if(!host) return;
  const all=_eventsData||[];
  const COL={earnings:"#9b87d4",expiry:"#d8a84b",rebalance:"#46b8ad"};
  const pills=EVT_FILTERS.map(([k,l])=>{ const on=_evtFilter===k, n=k==="all"?all.length:all.filter(e=>e.type===k).length;
    return `<button onclick="setEvtFilter('${k}')" class="evtf${on?' on':''}">${l}${k!=="all"?` <span style="opacity:.55">${n}</span>`:''}</button>`; }).join("");
  const filterRow=`<div style="display:flex;gap:5px;flex-wrap:wrap;padding:10px 14px 6px;border-bottom:1px solid var(--line-soft)">${pills}</div>`;
  const ev=(_evtFilter==="all"?all:all.filter(e=>e.type===_evtFilter)).slice(0,7);
  let list;
  if(!ev.length){ list=`<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">no ${_evtFilter==="all"?"":(EVT[_evtFilter]||{}).label?.toLowerCase()+" "||""}events upcoming</div>`; }
  else { list=ev.map(e=>{ const secs=evtSecsLeft(e.date),col=COL[e.type]||"#65758c",live=secs<7*86400;
    const label=(e.symbol?e.symbol+" ":"")+((EVT[e.type]||{}).label||e.type)+(e.detail&&e.type==='expiry'?" · "+e.detail:"");
    return `<div style="display:flex;align-items:center;gap:11px;padding:9px 16px;border-bottom:1px solid var(--line-soft)"><span style="width:7px;height:7px;border-radius:50%;background:${col};flex:none"></span><div style="flex:1;min-width:0"><div style="font:500 12px 'Space Grotesk';color:var(--fg-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${label}</div><div style="font:400 10px var(--mono);color:var(--muted)">${mdy(e.date)}</div></div><span style="font:500 12px var(--mono);color:${live?'#46b8ad':'#aab4c3'}">${hms(secs)}</span></div>`; }).join(""); }
  host.innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Events</span><span class="ovhs">countdown · live${srcInfo('events')}</span></div>${filterRow}<div style="max-height:280px;overflow-y:auto" class="sf-scroll">${list}</div></div>`;
}

function renderWaterfall(s){
  const host=$("ov-waterfall"); if(!host||s.nav==null) return;
  // Premium = option premium booked today (options_lifecycle); Costs = fees booked today
  // (Alpaca posts them next-morning, so this usually pays for yesterday's trading).
  // Price is the residual, so the three bars always sum to the day move.
  const at=_attr||{}, premium=at.premium_today||0, costs=-(at.costs_today||0);
  const nav=s.nav, dayPnl=s.day_pnl||0, prior=nav-dayPnl, price=dayPnl-premium-costs;
  const steps=[["Prior NAV",prior,"base"],["Price",price,"d"],["Premium",premium,"d"],["Costs",costs,"d"],["Live NAV",nav,"base"]];
  const W=980,H=210,pl=10,pr=10,pb=34,pt=24,pw=W-pl-pr,ph=H-pt-pb;   // pt headroom so the top bar's value label isn't clipped
  const vals=[prior,nav]; let run=prior; [price,premium,costs].forEach(v=>{ run+=v; vals.push(run); });
  let lo=Math.min(...vals),hi=Math.max(...vals); const pd=(hi-lo)*0.4||1; lo-=pd; const span=hi-lo||1;
  const Y=v=>pt+(hi-v)/span*ph, gap=pw/steps.length, bw=gap*0.5;
  let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:210px">`; let cum=prior;
  steps.forEach((st,i)=>{ const x=pl+gap*i+(gap-bw)/2; let top,bot,col;
    if(st[2]==="base"){ top=Y(st[1]); bot=Y(lo); col="#3a4a66"; cum=st[1]; }
    else { const start=cum,end=cum+st[1]; top=Y(Math.max(start,end)); bot=Y(Math.min(start,end)); col=st[1]>=0?"#5fb088":"#cf6f66"; cum=end;
      const px2=pl+gap*(i-1)+(gap-bw)/2+bw; g+=`<line x1="${px2}" y1="${Y(start).toFixed(1)}" x2="${x.toFixed(1)}" y2="${Y(start).toFixed(1)}" stroke="#2a3a58" stroke-dasharray="2 2"/>`; }
    g+=`<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(2,bot-top).toFixed(1)}" rx="2" fill="${col}"/>`;
    const lv=st[2]==="base"?usd(st[1]):((st[1]>=0?"+":"-")+"$"+fmt(Math.abs(st[1]),0));
    g+=`<text x="${(x+bw/2).toFixed(1)}" y="${(top-5).toFixed(1)}" text-anchor="middle" font-size="11" fill="#cdd9e8" font-family="IBM Plex Mono">${lv}</text>`;
    g+=`<text x="${(x+bw/2).toFixed(1)}" y="${H-12}" text-anchor="middle" font-size="11" fill="#65758c" font-family="Space Grotesk">${st[0]}</text>`; });
  host.innerHTML=ovPanel("P&L attribution — today","price vs premium vs costs", g+"</svg>", "10px 12px 6px", "attribution");
}

// Repaint the whole Overview from the latest state (called on load + every 1s tick).
// ---- Chase board (Phase 2): each order's child limit walking the bid→ask spread, per round ----
// One track per working name: bid at the left, ask at the right, a faint mid tick, and a marker at
// the posted limit that walks toward the touch as the ladder climbs (buy = teal→ask, sell = red→bid).
function chaseTrack(o){
  const lo=o.bid, hi=o.ask, lp=o.limit_price, mid=o.mid, buy=o.side!=='sell';
  const hasSpread = (lo!=null && hi!=null && hi>lo);
  const fr = v => (hasSpread && v!=null) ? Math.max(0,Math.min(1,(v-lo)/(hi-lo))) : 0.5;
  const mc = buy ? '#46b8ad' : '#cf6f66';
  const filled = o.status==='filled' || (o.fill_pct>=0.999);
  const rejected = o.status==='rejected';
  const rtook = (o.n_rounds||0)>1 ? ` · ${o.n_rounds}r` : '';
  const statusTxt = rejected?'rejected':filled?('filled'+rtook):(o.round||'working');
  const statusCol = rejected?'var(--red)':filled?'var(--green)':'var(--amber)';
  const fillW = Math.round((o.fill_pct||0)*100);
  const track = hasSpread
    ? `<div style="position:relative;height:18px;border-radius:4px;background:linear-gradient(90deg,#0c1626,#12203a);border:1px solid var(--line-soft)">`
      +`<span style="position:absolute;left:${(fr(mid)*100).toFixed(1)}%;top:2px;bottom:2px;width:1px;background:#33415e"></span>`
      +`<span style="position:absolute;left:${(fr(lp)*100).toFixed(1)}%;top:-1px;bottom:-1px;width:2px;background:${mc};box-shadow:0 0 6px ${mc};transform:translateX(-1px);transition:left .5s var(--ease)"></span>`
      +`<span style="position:absolute;left:5px;top:4px;font:500 10px var(--mono);color:var(--muted)">${usd2(lo)}</span>`
      +`<span style="position:absolute;right:5px;top:4px;font:500 10px var(--mono);color:var(--muted)">${usd2(hi)}</span></div>`
    : `<div style="height:18px;border-radius:4px;background:#0c1626;display:flex;align-items:center;justify-content:center;font:400 10px var(--mono);color:var(--muted)">${lp!=null?'limit '+usd2(lp):'—'}</div>`;
  const statusTip = rejected ? 'the broker rejected this order — deferred to the cross-day queue'
    : filled ? `filled${(o.n_rounds||0)>1?` after ${o.n_rounds} ladder rounds`:''}`
    : `limit ladder round ${String(o.round||'').replace(/^r/,'')||'1'} — the child limit re-pegs from the mid toward the touch each round until it fills`;
  return `<div style="display:grid;grid-template-columns:50px 1fr 92px;gap:10px;align-items:center;padding:5px 0">`
    +`<span style="font:600 10px var(--mono);color:var(--fg-dim)">${o.symbol}</span>`
    +`<div>${track}<div style="height:3px;margin-top:3px;border-radius:2px;background:#0c1626;overflow:hidden"><span style="display:block;height:100%;width:${fillW}%;background:${mc};opacity:.65;transition:width .4s var(--ease)"></span></div></div>`
    +`<span style="text-align:right;font:500 10px 'Space Grotesk';letter-spacing:.02em;text-transform:uppercase;color:${statusCol}" data-tip="${statusTip}">${statusTxt}</span></div>`;
}
function chaseBoard(ex){
  const orders=(ex.chase||[]).filter(o=>o.bid!=null||o.limit_price!=null).slice(0,10);
  if(!orders.length) return "";
  return `<div style="border-top:1px solid var(--line-soft);padding:10px 16px 12px">`
    +`<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:5px"><span style="font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted)">Chase board</span><span style="font:400 10px var(--mono);color:var(--muted);opacity:.75">bid ← limit walks → ask</span></div>`
    +orders.map(chaseTrack).join("")+`</div>`;
}

// ---- Rotation flow (Phase 3): a Sankey of equity sells → cash → buys for the live cycle -------
// Each filled sell (left, soft-red) feeds the central CASH node; each filled buy (right, teal)
// draws from it. Ribbon widths ∝ filled notional, so the capital rotation reads at a glance.
function rotationFlow(ex){
  const r=ex.rotation||{}, sells=(r.sells||[]).slice(0,7), buys=(r.buys||[]).slice(0,7);
  if(!sells.length && !buys.length) return "";
  const W=980,H=210,top=22,usableH=H-top-46,nodeW=13,leftX=62,rightX=W-62-nodeW,cashX=W/2-nodeW/2;
  const raised=r.raised||sells.reduce((a,s)=>a+s.notional,0);
  const deployed=r.deployed||buys.reduce((a,b)=>a+b.notional,0);
  const scale=usableH/Math.max(raised,deployed,1), cashH=Math.max(raised,deployed)*scale;
  const abbr=v=>'$'+(v>=1000?(v/1000).toFixed(v>=1e4?0:1)+'k':Math.round(v));
  const rib=(x0,y0,x1,y1,w,c)=>{ const cx=(x0+x1)/2;
    return `<path d="M${x0},${y0.toFixed(1)} C${cx},${y0.toFixed(1)} ${cx},${y1.toFixed(1)} ${x1},${y1.toFixed(1)} L${x1},${(y1+w).toFixed(1)} C${cx},${(y1+w).toFixed(1)} ${cx},${(y0+w).toFixed(1)} ${x0},${(y0+w).toFixed(1)} Z" fill="${c}" fill-opacity="0.26"/>`; };
  let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:210px">`;
  let ly=top, cy=top;
  sells.forEach(s=>{ const h=Math.max(2,s.notional*scale);
    g+=rib(leftX+nodeW, ly, cashX, cy, h, "#cf6f66")
      +`<rect x="${leftX}" y="${ly.toFixed(1)}" width="${nodeW}" height="${h.toFixed(1)}" rx="2" fill="#cf6f66"/>`
      +`<text x="${leftX-7}" y="${(ly+h/2+3.5).toFixed(1)}" text-anchor="end" font-size="10.5" fill="#aab4c3" font-family="IBM Plex Mono">${s.symbol}</text>`;
    ly+=h+6; cy+=h; });
  let ry=top, oy=top;
  buys.forEach(b=>{ const h=Math.max(2,b.notional*scale);
    g+=rib(cashX+nodeW, oy, rightX, ry, h, "#46b8ad")
      +`<rect x="${rightX}" y="${ry.toFixed(1)}" width="${nodeW}" height="${h.toFixed(1)}" rx="2" fill="#46b8ad"/>`
      +`<text x="${rightX+nodeW+7}" y="${(ry+h/2+3.5).toFixed(1)}" text-anchor="start" font-size="10.5" fill="#aab4c3" font-family="IBM Plex Mono">${b.symbol}</text>`;
    ry+=h+6; oy+=h; });
  g+=`<rect x="${cashX}" y="${top}" width="${nodeW}" height="${Math.max(2,cashH).toFixed(1)}" rx="2" fill="#7c8bb0"/>`
    +`<text x="${(cashX+nodeW/2).toFixed(1)}" y="${(top+cashH+15).toFixed(1)}" text-anchor="middle" font-size="10" fill="#8695ad" font-family="Space Grotesk" letter-spacing="1.5">CASH</text>`
    +`<text x="${leftX-7}" y="${(H-10)}" text-anchor="end" font-size="10" fill="#65758c" font-family="Space Grotesk">sold ${abbr(raised)}</text>`
    +`<text x="${rightX+nodeW+7}" y="${(H-10)}" text-anchor="start" font-size="10" fill="#65758c" font-family="Space Grotesk">bought ${abbr(deployed)}</text></svg>`;
  return `<div style="border-top:1px solid var(--line-soft);padding:9px 16px 6px">`
    +`<div style="font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:2px">Rotation — cash → names</div>${g}</div>`;
}

// ---- Animated fills tape (Phase 3): a live marquee of recent fills, buys teal / sells red -------
// The negative animation-delay resumes the scroll at the right phase after each 2.5s repaint, so
// the tape reads as continuous rather than restarting; the content is doubled for a seamless wrap.
function fillsTape(ex){
  const fl=(ex.recent_fills||[]).slice(0,12);
  if(!fl.length) return "";
  const chip=f=>`<span style="margin:0 15px;white-space:nowrap"><b style="color:${f.side==='sell'?'#cf6f66':'#5fb088'}">${f.symbol}</b> <span style="color:var(--fg-dim)">${fmt(f.qty)}</span> <span style="color:var(--muted)">@</span> <span style="color:var(--fg-dim)">${f.price!=null?(+f.price).toFixed(2):'—'}</span></span>`;
  const seq=fl.map(chip).join("");
  const t=(typeof performance!=='undefined'?performance.now():Date.now())/1000;
  return `<div style="border-top:1px solid var(--line-soft);padding:9px 16px;display:flex;align-items:center;gap:12px;overflow:hidden">`
    +`<span style="display:inline-flex;align-items:center;gap:6px;flex:none"><span style="width:6px;height:6px;border-radius:50%;background:#5fb088;animation:pulse 1.8s infinite"></span><span style="font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted)">Fills</span></span>`
    +`<div style="overflow:hidden;flex:1;-webkit-mask-image:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent);mask-image:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent)">`
    +`<div style="display:inline-flex;white-space:nowrap;font:400 11px var(--mono);animation:tape-scroll 42s linear infinite;animation-delay:-${(t%42).toFixed(2)}s">${seq}${seq}</div></div></div>`;
}

// ---- Live execution visualizer (Phase 1: run-progress + blotter; auto-surfaces during trading) ----
function renderExec(ex){
  const host=$("x-exec"); if(!host) return;
  if(!ex || !ex.active){ host.style.display="none"; host.innerHTML=""; return; }
  host.style.display="block";
  // Label the option leg by the active overlay: SPY spread overwrite (index) vs per-name calls.
  const idxOv = !(_overlay && _overlay.mode==="per_name");
  const writeLbl = idxOv ? "Write SPY spread" : "Write calls";
  const closeLbl = idxOv ? "Close spread" : "Close calls";
  const phaseName={equity_chase:"Equity chase",writing_calls:idxOv?"Writing SPY spread":"Writing calls",idle:"Settling"}[ex.phase]||"Trading";
  const curIdx = ex.phase==="equity_chase"?1 : ex.phase==="writing_calls"?2 : 3;
  // Manual console runs get their own phase strip + header (the rebalance strip would lie).
  const man = ex.manual, mp = (man && man.params) || {};
  const manSteps = man && ({liquidate:[["Trim spread",0],["Equity sells",1],["Settle",2]],
                            leverage:[["Overlay",0],["Equity scale",1],["Settle",2]],
                            trade:[["Order",0],["Settle",1]]})[man.action];
  // Manual phase from real state: nothing posted yet → step 0; orders working → the equity
  // step; posted-and-none-working → settling. (Was a crude n_working ternary that showed
  // "Settle" for the first seconds of a run.)
  const manIdx = manSteps ? Math.min(
    ex.n_working > 0 ? manSteps.length - 2 : ((ex.chase||[]).length ? manSteps.length - 1 : 0),
    manSteps.length - 1) : 0;
  const stripSrc = (man && man.action!=="rebalance" && manSteps)
    ? manSteps.map(([l,i])=>[l,i,i<manIdx,i===manIdx])
    : [[closeLbl,0],["Equity chase",1],[writeLbl,2],["Snapshot",3]].map(([l,i])=>[l,i,i<curIdx,i===curIdx]);
  const strip=stripSrc.map(([l,,done,cur])=>{
    const mark=done?"✓":cur?"●":"○";
    const c=done?"var(--green)":cur?"var(--fg)":"var(--muted)";
    return `<span style="color:${c};font:500 11px 'Space Grotesk'">${mark} ${l}</span>`; }).join("");
  const pct = ex.n_target ? Math.round(ex.n_filled/ex.n_target*100) : 0;
  const lev=ex.leverage, tlev=ex.target_leverage;
  const scol={filled:"var(--green)",held:"var(--green)",working:"var(--amber)",pending:"var(--muted)"};
  const names=(ex.names||[]).map(n=>{ const c=scol[n.status]||"var(--muted)", fw=Math.round((n.fill_pct||0)*100);
    return `<div style="display:grid;grid-template-columns:58px 1fr 56px;gap:8px;align-items:center;padding:4px 0">`
      +`<span style="font:600 10px var(--mono);color:var(--fg-dim)">${n.symbol}</span>`
      +`<span style="height:8px;border-radius:4px;background:#0c1626;overflow:hidden;display:block"><span style="display:block;height:100%;width:${fw}%;background:${c};transition:width .4s var(--ease)"></span></span>`
      +`<span style="text-align:right;font:600 10px 'Space Grotesk';letter-spacing:.03em;text-transform:uppercase;color:${c}">${n.status}</span></div>`; }).join("");
  const body=`<div style="display:flex;gap:18px;flex-wrap:wrap;padding:11px 16px;border-bottom:1px solid var(--line-soft)">${strip}</div>`
    +`<div style="display:flex;align-items:center;gap:14px;padding:12px 16px">`
    +`<span style="font:500 13px var(--mono);color:var(--fg);white-space:nowrap">${ex.n_filled} / ${ex.n_target} <span style="color:var(--muted);font-size:11px">filled</span></span>`
    +`<div style="flex:1;min-width:120px;height:8px;border-radius:5px;background:var(--panel-2);overflow:hidden;position:relative"><div style="position:absolute;left:0;top:0;height:100%;width:${pct}%;background:linear-gradient(90deg,#2a6f63,#46b8ad);transition:width .4s var(--ease)"></div></div>`
    +`<span style="font:500 12px var(--mono);color:var(--fg-dim);white-space:nowrap">${lev!=null?lev.toFixed(2):'–'}× <span style="color:var(--muted)">→ ${tlev?tlev.toFixed(2):'–'}×</span></span>`
    +`<span style="font:500 11px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:var(--amber);white-space:nowrap">${ex.n_working} working</span></div>`
    +(names?`<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:0 24px;padding:2px 16px 12px">${names}</div>`:"")
    +chaseBoard(ex)
    +rotationFlow(ex)
    +fillsTape(ex);
  const manLbl = man && ({rebalance:`Manual rebalance`,
                          liquidate:`Liquidation ${mp.pct!=null?(+mp.pct)+"%":""}`,
                          leverage:`Leverage → ${mp.target!=null?(+mp.target).toFixed(2)+"×":""}`,
                          trade:`Manual ${(mp.side||"trade").toUpperCase()} ${mp.symbol||""}`}[man.action] || "Manual action");
  const title=`<span style="display:inline-flex;align-items:center;gap:7px"><span style="width:8px;height:8px;border-radius:50%;background:${man?"#d9a441":"#5fb088"};box-shadow:0 0 0 3px ${man?"rgba(217,164,65,.18)":"rgba(95,176,136,.18)"}"></span>${man?manLbl:"Live rebalance"}${man?` <span style="font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:2px 6px">${man.mode}</span>`:""}</span>`;
  host.innerHTML=ovPanel(title, phaseName, body, "0", "orders");
}
async function pollExec(){ try{ renderExec(await get("/api/execution")); }catch(e){} }

// Repaint memo (audit W2): the 1s tick used to rebuild every panel's innerHTML — including
// the multi-hundred-node treemap SVG — even when NOTHING changed (identical for ~17h after
// hours). Each heavy painter now runs only when its input signature changes: after-hours
// everything below is skipped; in-session a panel repaints exactly when a price it shows
// actually moved. The tape/hero/ribbon keep their unconditional 1s feel (cheap or in-place).
const _paintMemo = {};
function memoPaint(key, sig, fn){
  const j = JSON.stringify(sig);
  if (_paintMemo[key] === j) return;
  _paintMemo[key] = j;
  fn();
}

function paintOverview(s){
  _ovS=s; pushPx(s); renderTape(s);
  renderSession(); renderNavHero(s); renderKpiRibbon(s);
  memoPaint('treemap',
    (s.positions||[]).map(p=>[p.symbol, p.market_value,
                              p.day_pct==null?null:+p.day_pct.toFixed(4)]),
    () => renderTreemap(s));
  memoPaint('gauge',
    (_overlay && _overlay.mode !== "per_name")
      ? ['idx', _overlay.short_symbol, _overlay.contracts, _overlay.beta_overwritten,
         _overlay.net_credit, _overlay.expiration,
         _overlay.spot==null?null:Math.round(_overlay.spot)]
      : ['pn', (_calls||[]).map(c=>[c.option_symbol,c.contracts]),
         (s.positions||[]).map(p=>[p.symbol, p.qty, p.market_value==null?null:Math.round(p.market_value)])],
    () => renderGauge(s, _calls, _overlay));
  renderEventsPanel();                                   // countdowns tick every second by design
  memoPaint('waterfall',
    [s.nav==null?null:Math.round(s.nav), s.day_pnl==null?null:Math.round(s.day_pnl),
     _attr && _attr.premium_today, _attr && _attr.costs_today],
    () => renderWaterfall(s));
  renderFootstrip(s);
}

// Overview — the daily glance (design-doc layout)
async function loadOverview(){
  const [s, hist, calls, ev, ov, at] = await Promise.all(
    ["/api/state","/api/nav_history?limit=1000","/api/calls","/api/events","/api/overlay",
     "/api/attribution"].map(get));
  _attr = at || _attr;
  noteFresh(s);
  if(!s){ $("ov-navhero").innerHTML =
      '<div class="errbox"><div class="big">Backend unreachable</div>Postgres or the dashboard API is not responding.</div>';
    return; }
  _navBase = hist || []; _calls = calls || []; _eventsData = (ev && ev.events) || []; _overlay = ov || null;
  paintOverview(s);
}

// ===================== Portfolio — design-doc layout =====================
let _pFactors = [];
function fillPanel(id,title,sub,inner,src){ const el=$(id); if(el) el.innerHTML=`<div class="ovhead"><span class="ovhk">${title}</span><span class="ovhs">${sub||''}${src?srcInfo(src):''}</span></div>${inner}`; }
const scol=v=>v==null?'var(--muted)':v>1e-9?'var(--green)':v<-1e-9?'var(--red)':'var(--muted)';
const zcol=v=>v==null?'var(--muted)':v>0.05?'var(--green)':v<-0.05?'var(--red)':'var(--fg-dim)';
const sz=v=>v==null?'–':(v>=0?'+':'')+v.toFixed(2);

function renderPSector(s){
  const rows=(s.positions||[]).filter(p=>p.symbol&&!_isOpt(p.symbol)&&p.weight);
  if(!rows.length){ fillPanel("p-sector","Sector exposure","",'<div style="padding:16px;color:var(--muted);font:400 12px var(--mono)">no holdings yet</div>',"live"); return; }
  const tot=rows.reduce((a,p)=>a+(p.weight||0),0)||1, cap=META.max_sector_pct||0.30;
  const bySec={}; rows.forEach(p=>{ const sec=(REF[p.symbol.toUpperCase()]||{}).sector||'—'; (bySec[sec]=bySec[sec]||{w:0,n:0}); bySec[sec].w+=p.weight; bySec[sec].n++; });
  const secs=Object.entries(bySec).map(([name,v])=>({name,w:v.w/tot,n:v.n})).sort((a,b)=>b.w-a.w);
  const bar=`<div style="display:flex;height:14px;border-radius:3px;overflow:hidden;gap:1px;background:var(--bg)">${secs.map(x=>`<div style="width:${(x.w*100).toFixed(2)}%;background:${secColor(x.name)}" data-tip="${x.name} ${pct(x.w)} · ${x.n} name${x.n>1?'s':''}"></div>`).join('')}</div>`;
  const legend=`<div style="display:flex;flex-wrap:wrap;gap:10px 22px;margin-top:16px">${secs.map(x=>`<span style="display:inline-flex;align-items:center;gap:8px;font:400 11.5px 'Space Grotesk';color:var(--fg-dim)"><span style="width:9px;height:9px;border-radius:2px;background:${secColor(x.name)};flex:none"></span>${x.name} <b class="pmono" style="font-weight:500;color:${x.w>cap+1e-9?'var(--red)':'var(--fg-dim)'}">${pct(x.w)}${x.w>cap+1e-9?' ▲':''}</b> <span class="pmono" style="font-size:10px;color:var(--muted)">${x.n}</span></span>`).join('')}</div>`;
  const ws=rows.map(p=>p.weight/tot).sort((a,b)=>b-a);
  const top5=ws.slice(0,5).reduce((a,b)=>a+b,0), eff=1/ws.reduce((a,b)=>a+b*b,0), largest=ws[0]||0;
  const foot=`<div style="display:flex;flex-wrap:wrap;gap:8px 30px;margin-top:16px;padding-top:14px;border-top:1px solid var(--line-soft);font:400 11.5px 'Space Grotesk';color:var(--muted)"><span>Top 5 names <b class="pmono" style="font-weight:500;color:var(--fg-dim)">${pct(top5)}</b></span><span>Effective names <b class="pmono" style="font-weight:500;color:var(--fg-dim)">${eff.toFixed(1)}</b> of ${rows.length}</span><span>Largest <b class="pmono" style="font-weight:500;color:var(--fg-dim)">${pct(largest)}</b></span><span>Sectors <b class="pmono" style="font-weight:500;color:var(--fg-dim)">${secs.length}</b></span></div>`;
  fillPanel("p-sector","Sector exposure",`share of book · sectors over ${(cap*100).toFixed(0)}% cap flagged`,`<div style="padding:16px 18px">${bar}${legend}${foot}</div>`,"live");
}

function renderPPositions(s){
  const rows=(s.positions||[]).filter(p=>p.symbol&&!_isOpt(p.symbol)&&p.qty);
  const G="display:grid;grid-template-columns:1.8fr .75fr .72fr .95fr 1fr 1.35fr .8fr .62fr;gap:10px;align-items:center";
  const maxW=Math.max(0.01,...rows.map(p=>p.weight||0),...rows.map(p=>p.target_weight||0));
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Instrument</span><span style="text-align:right">Last</span><span style="text-align:right">Day</span><span>20-tick</span><span style="text-align:right">Mkt value</span><span>Allocation vs target</span><span style="text-align:right">Δ vs tgt</span><span style="text-align:right">Trade</span></div>`;
  const body=rows.map(p=>{ const chg=p.day_pct,c=chg==null?'var(--muted)':chg>=0?'var(--green)':'var(--red)';
    const px=p.last_price!=null?p.last_price:(p.qty?p.market_value/p.qty:null),r=REF[p.symbol.toUpperCase()]||{};
    const spk=sparkP((_px[p.symbol]||[]).slice(-16),92,22,2),d=(p.weight!=null&&p.target_weight!=null)?p.weight-p.target_weight:null;
    const fillW=Math.min(100,(p.weight||0)/maxW*100),tickW=Math.min(100,(p.target_weight||0)/maxW*100);
    return `<div class="prow" style="${G};padding:8px 16px">
      <span style="display:flex;align-items:center;gap:10px;min-width:0">${tkrChip(p.symbol,r.sector)}<span style="display:flex;flex-direction:column;min-width:0"><span style="font:500 12.5px 'Space Grotesk';color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.name||p.symbol}</span><span style="font:400 10px 'Space Grotesk';color:var(--muted)">${r.sector||'—'}</span></span></span>
      <span class="pmono" style="text-align:right;font-size:12px;color:var(--fg)">${px==null?'–':px.toFixed(2)}</span>
      <span class="pmono" style="text-align:right;font-size:12px;font-weight:500;color:${c}">${chg==null?'–':spct(chg)}</span>
      <span>${spk?`<svg width="92" height="22" viewBox="0 0 92 22" preserveAspectRatio="none" style="display:block"><path d="${spk}" fill="none" stroke="${c}" stroke-width="1.4" stroke-linejoin="round"/></svg>`:'<span style="color:var(--muted)">–</span>'}</span>
      <span class="pmono" style="text-align:right;font-size:12px;color:var(--fg)">${usd(p.market_value)}</span>
      <span style="display:flex;align-items:center;gap:9px"><span style="position:relative;flex:1;height:5px;border-radius:2px;background:var(--panel-2)"><span style="position:absolute;left:0;top:0;height:100%;border-radius:2px;background:var(--accent);width:${fillW.toFixed(1)}%"></span><span style="position:absolute;top:-2px;width:2px;height:9px;background:var(--fg-dim);left:${tickW.toFixed(1)}%"></span></span><span class="pmono" style="font-size:11px;color:var(--fg-dim);min-width:42px;text-align:right">${pct(p.weight)}</span></span>
      <span class="pmono" style="text-align:right;font-size:12px;font-weight:500;color:${scol(d)}">${d==null?'–':spct(d,1)}</span>
      <span style="display:flex;gap:4px;justify-content:flex-end"><button class="mini-trade buy" title="buy ${p.symbol}" onclick="openTicket('trade',{symbol:'${p.symbol}',side:'buy'})">B</button><button class="mini-trade sell" title="sell ${p.symbol}" onclick="openTicket('trade',{symbol:'${p.symbol}',side:'sell'})">S</button></span></div>`; }).join("");
  fillPanel("p-positions","Positions vs target",`${rows.length} equity positions · bar = weight, tick = target · B/S opens the ticket`,head+body,"live");
}

function renderPFactorTilt(s,factors){
  const fmap={}; (factors||[]).forEach(f=>fmap[f.symbol]=f);
  const rows=(s.positions||[]).filter(p=>p.symbol&&!_isOpt(p.symbol)&&p.weight&&fmap[p.symbol]);
  const held=(s.positions||[]).filter(p=>!_isOpt(p.symbol)&&p.qty).length;
  if(!rows.length){ fillPanel("p-ftilt","Factor tilt","book-weighted z-score",'<div style="padding:16px;color:var(--muted);font:400 12px var(--mono)">factor tilt appears once the book is scored (after the next rebalance)</div>',"factors"); return; }
  const sub={quality:0,value:0,beta:0,lowvol:0}; let tw=0;
  rows.forEach(p=>{ tw+=p.weight; ["quality","value","beta","lowvol"].forEach(k=>{ if(fmap[p.symbol][k]!=null) sub[k]+=p.weight*fmap[p.symbol][k]; }); });
  if(tw>0) Object.keys(sub).forEach(k=>sub[k]/=tw);
  const keys=[["quality","Quality","profitability & balance sheet"],["value","Value","cheapness on fundamentals"],["beta","Low Beta","lower market sensitivity (β vs SPY)"],["lowvol","Low Vol","lower realized volatility"]];
  const barFor=z=>{ const half=Math.min(1,Math.abs(z)/2)*50; return z>=0?`left:50%;width:${half.toFixed(1)}%`:`left:${(50-half).toFixed(1)}%;width:${half.toFixed(1)}%`; };
  const header=`<div style="display:grid;grid-template-columns:200px 1fr 56px;gap:16px;font:400 10px 'Space Grotesk';color:var(--muted);margin-bottom:8px"><span></span><span style="display:flex;justify-content:space-between"><span>‹ leans away</span><span>neutral</span><span>leans in ›</span></span><span></span></div>`;
  const rowsHtml=keys.map(([k,label,desc])=>{ const z=sub[k],c=zcol(z);
    return `<div style="display:grid;grid-template-columns:200px 1fr 56px;gap:16px;align-items:center;padding:9px 0;border-top:1px solid var(--line-soft)"><span style="font:500 12.5px 'Space Grotesk';color:var(--fg-dim)">${label} <span style="color:var(--muted);font-weight:400">· ${desc}</span></span><span style="position:relative;height:11px;background:var(--panel-2);border-radius:5px"><span style="position:absolute;top:-4px;bottom:-4px;left:50%;width:0;border-left:1px dashed var(--faint)"></span><span style="position:absolute;top:0;height:11px;border-radius:4px;background:${c};${barFor(z)}"></span></span><span class="pmono" style="text-align:right;font-weight:600;font-size:13px;color:${c}">${sz(z)}</span></div>`; }).join("");
  const comp=(sub.quality+sub.value+sub.beta+sub.lowvol)/4;
  const foot=`<div style="display:flex;flex-wrap:wrap;gap:8px 30px;margin-top:14px;padding-top:13px;border-top:1px solid var(--line-soft);font:400 11.5px 'Space Grotesk';color:var(--muted)"><span>Composite tilt <b class="pmono" style="font-weight:600;color:${zcol(comp)}">${sz(comp)}</b></span><span>Names scored <b class="pmono" style="font-weight:500;color:var(--fg-dim)">${rows.length}</b> of ${held}</span></div>`;
  fillPanel("p-ftilt","Factor tilt","book-weighted z-score · 0 = universe average",`<div style="padding:12px 18px 16px">${header}${rowsHtml}${foot}</div>`,"factors");
}

function renderPRadar(s,factors){
  const fmap={}; (factors||[]).forEach(f=>fmap[f.symbol]=f);
  const rows=(s.positions||[]).filter(p=>p.symbol&&!_isOpt(p.symbol)&&p.weight&&fmap[p.symbol]);
  const t={quality:0,value:0,beta:0,lowvol:0}; let tw=0;
  rows.forEach(p=>{ tw+=p.weight; ["quality","value","beta","lowvol"].forEach(k=>{ if(fmap[p.symbol][k]!=null) t[k]+=p.weight*fmap[p.symbol][k]; }); });
  if(tw>0) Object.keys(t).forEach(k=>t[k]/=tw);
  const axes=[["Quality",t.quality],["Value",t.value],["Low Beta",t.beta],["Low Vol",t.lowvol]];
  const W=360,H=250,cx=180,cy=124,R=84,ang=i=>(-90+i*90)*Math.PI/180,map=z=>Math.max(0.06,Math.min(1,(z+1)/2.2));
  const ring=rad=>axes.map((_,i)=>[cx+rad*Math.cos(ang(i)),cy+rad*Math.sin(ang(i))]);
  const poly=(pts,fill,stroke,sw)=>`<polygon points="${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
  const grid=cssv('--line','#1b2740'),acc=cssv('--accent','#46b8ad');
  let g=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:250px">`;
  [0.33,0.66,1].forEach(f=>{ g+=poly(ring(R*f),"none",grid,1); });
  axes.forEach((_,i)=>{ const p=[cx+R*Math.cos(ang(i)),cy+R*Math.sin(ang(i))]; g+=`<line x1="${cx}" y1="${cy}" x2="${p[0].toFixed(1)}" y2="${p[1].toFixed(1)}" stroke="${grid}"/>`; });
  g+=poly(ring(R*map(0)),"none","#2a3a58",1);
  const bp=axes.map((a,i)=>{ const rad=R*map(a[1]); return [cx+rad*Math.cos(ang(i)),cy+rad*Math.sin(ang(i))]; });
  g+=poly(bp,"rgba(70,184,173,.18)",acc,2); bp.forEach(p=>{ g+=`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.6" fill="${acc}"/>`; });
  const la=['text-anchor="middle"','text-anchor="start"','text-anchor="middle"','text-anchor="end"'];
  axes.forEach((a,i)=>{ const p=[cx+(R+16)*Math.cos(ang(i)),cy+(R+16)*Math.sin(ang(i))+3]; g+=`<text x="${p[0].toFixed(1)}" y="${p[1].toFixed(1)}" ${la[i]} font-size="10.5" fill="#aab4c3" font-family="Space Grotesk" font-weight="500">${a[0]}</text>`; });
  g+="</svg>";
  const leg=`<div style="display:flex;gap:16px;justify-content:center;margin-top:4px"><span style="display:inline-flex;align-items:center;gap:6px;font:400 10.5px 'Space Grotesk';color:#aab4c3"><span style="width:12px;height:3px;background:${acc};border-radius:2px"></span>Book tilt</span><span style="display:inline-flex;align-items:center;gap:6px;font:400 10.5px 'Space Grotesk';color:var(--muted)"><span style="width:12px;height:3px;background:#2a3a58;border-radius:2px"></span>Universe</span></div>`;
  $("p-radar").innerHTML=`<div class="ovpanel"><div class="ovhead"><span class="ovhk">Factor radar</span><span class="ovhs">book-weighted z${srcInfo('factors')}</span></div><div style="padding:10px 14px 14px">${rows.length?g+leg:'<div style=\"color:var(--muted);font:400 12px var(--mono);padding:6px 4px\">appears once scored</div>'}</div></div>`;
}

function renderPCalls(s,calls,ov){
  // SPY beta-overwrite spread (index overlay): a defined-risk detail card, not a per-name table.
  if(ov && ov.mode!=="per_name"){ renderPOverlay(s,ov); return; }
  const G="display:grid;grid-template-columns:1.3fr .6fr .8fr .8fr .55fr .8fr;gap:8px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Underlying</span><span style="text-align:right">Ct</span><span style="text-align:right">Strike</span><span style="text-align:right">DTE</span><span style="text-align:right">Δ</span><span style="text-align:right">Premium</span></div>`;
  let body;
  if(!(calls&&calls.length)) body=`<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">No calls written — the overlay sells ~0.30-delta calls on ≥100-share holdings at each rebalance.</div>`;
  else body=`<div class="sf-scroll" style="max-height:340px;overflow:auto">${calls.map(c=>{ const d=dte(c.expiration),r=REF[(c.underlying||'').toUpperCase()]||{};
    return `<div class="prow" style="${G};padding:8px 16px"><span style="display:flex;align-items:center;gap:8px;min-width:0">${tkrChip(c.underlying,r.sector)}<span style="font:400 10px 'Space Grotesk';color:var(--amber);letter-spacing:.04em;text-transform:uppercase">call</span></span><span class="pmono" style="text-align:right;font-size:12px;color:var(--fg-dim)">${fmt(c.contracts)}</span><span class="pmono" style="text-align:right;font-size:12px;color:var(--fg-dim)">${usd2(c.strike)}</span><span class="pmono" style="text-align:right;font-size:12px;font-weight:500;color:${d!=null&&d<=7?'var(--amber)':'var(--fg-dim)'}">${d==null?'–':d+'d'}</span><span class="pmono" style="text-align:right;font-size:12px;color:var(--fg-dim)">${c.delta==null?'–':c.delta.toFixed(2)}</span><span class="pmono" style="text-align:right;font-size:12px;font-weight:500;color:var(--green)">${usd(c.premium)}</span></div>`; }).join('')}</div>`;
  const coveredSh=(calls||[]).reduce((a,c)=>a+(c.contracts||0)*100,0);
  fillPanel("p-calls","Covered calls",(calls&&calls.length)?`${calls.length} open · ${fmt(coveredSh)} sh covered`:"0 open",head+body,"calls");
}

// Portfolio-tab detail card for the SPY beta-overwrite vertical call spread.
function renderPOverlay(s,ov){
  if(!ov || !ov.active){
    const body=`<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted);line-height:1.55">No SPY spread open — the overlay writes one beta-sized SPY vertical call spread (short ~0.30Δ / long wing, same expiry) at each monthly rebalance.</div>`;
    fillPanel("p-calls","SPY beta overlay","0 open · β-sized spread",body,"overlay"); return;
  }
  const d=dte(ov.expiration);
  const legRow=(side,strike,col)=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--line-soft)">`
    +`<span style="display:flex;align-items:center;gap:9px">${tkrChip(ov.market||"SPY","ETF")}<span style="font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:${col}">${side}</span></span>`
    +`<span class="pmono" style="font-size:13px;color:var(--fg)">${strike==null?"–":fmt(strike,0)+"C"}</span></div>`;
  const kv=(k,v,c)=>`<div style="display:flex;flex-direction:column;gap:3px"><span style="font:600 10px 'Space Grotesk';letter-spacing:.07em;text-transform:uppercase;color:var(--muted)">${k}</span><span class="pmono" style="font-size:13px;color:${c||'var(--fg-dim)'}">${v}</span></div>`;
  const grid=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px 12px;padding:14px 16px">`
    +kv("Expiry",`${expShort(ov.expiration)} · ${d==null?"–":d+"d"}`)
    +kv("Spreads",fmt(ov.contracts))
    +kv("Short Δ",ov.short_delta==null?"–":ov.short_delta.toFixed(2))
    +kv("Net credit",ov.net_credit!=null?"$"+ov.net_credit.toFixed(2)+" / spread":"–","var(--green)")
    +kv("Credit collected",ov.premium_total!=null?usd(ov.premium_total):"–","var(--green)")
    +kv("Max risk (defined)",ov.max_risk!=null?usd(ov.max_risk):"–","#cf9a5f")
    +kv(`<span style="text-transform:none">β</span> overwritten`,ov.beta_overwritten!=null?ov.beta_overwritten.toFixed(2):"–")
    +kv("Gross overwritten",(ov.beta_overwritten!=null&&ov.gross_equity!=null)?usd(ov.contracts*100*(ov.spot||0)):"–")
    +`</div>`;
  const body=legRow("Short call",ov.short_strike,"var(--red)")+legRow("Long wing",ov.long_strike,"var(--green)")+grid;
  fillPanel("p-calls","SPY beta overlay",`1 spread · ${fmt(ov.contracts)} contracts`,body,"overlay");
}

function renderPFactors(factors){
  const G="display:grid;grid-template-columns:1.1fr .7fr .55fr .55fr .55fr .6fr;gap:6px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Symbol</span><span style="text-align:right">Comp</span><span style="text-align:right">Qual</span><span style="text-align:right">Val</span><span style="text-align:right">Beta</span><span style="text-align:right">LoVol</span></div>`;
  let body;
  if(!(factors&&factors.length)) body=`<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">Factor scores appear after the next rebalance scores the book.</div>`;
  else { const rows=[...factors].sort((a,b)=>(b.composite||0)-(a.composite||0));
    const cell=v=>`<span class="pmono" style="text-align:right;font-size:11.5px;color:${zcol(v)}">${sz(v)}</span>`;
    body=`<div class="sf-scroll" style="max-height:340px;overflow:auto">${rows.map(f=>{ const r=REF[f.symbol.toUpperCase()]||{};
      return `<div class="prow" style="${G};padding:7px 16px"><span>${tkrChip(f.symbol,r.sector)}</span><span class="pmono" style="text-align:right;font-size:12px;font-weight:600;color:${zcol(f.composite)}">${sz(f.composite)}</span>${cell(f.quality)}${cell(f.value)}${cell(f.beta)}${cell(f.lowvol)}</div>`; }).join('')}</div>`; }
  fillPanel("p-factors","Factor scores","held names · z-score",head+body,"factors");
}

function paintPortfolioLive(s){ pushPx(s); renderTape(s); renderPPositions(s); renderPCalls(s,_calls,_overlay); }

// Portfolio — Holdings (design-doc layout) + Activity (orders/alerts)
async function loadPortfolio(){
  const [s, orders, calls, factors, alerts, ov, manual] = await Promise.all(
    ["/api/state","/api/orders","/api/calls","/api/factors","/api/alerts","/api/overlay",
     "/api/manual_actions"].map(get));
  noteFresh(s);
  if(!s){ $("p-positions").innerHTML =
      '<div class="errbox"><div class="big">Backend unreachable</div>Postgres or the dashboard API is not responding.</div>';
    return; }
  _calls = calls || []; _pFactors = factors || []; _overlay = ov || null; pushPx(s); renderTape(s);
  renderPSector(s); renderPPositions(s); renderPFactorTilt(s,_pFactors); renderPRadar(s,_pFactors);
  renderPCalls(s,_calls,_overlay); renderPFactors(_pFactors); renderOrders(orders); renderAlerts(alerts);
  renderManualActions(manual);
}

function renderManualActions(rows, panelId){
  _manualRows = rows || [];
  const G="display:grid;grid-template-columns:150px 110px 70px 1fr 90px 1.2fr;gap:12px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Time (UTC)</span><span>Action</span><span>Mode</span><span>Params</span><span>Status</span><span>Result</span></div>`;
  let body;
  if(!(rows&&rows.length)) body='<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">No manual actions yet — the Execute tab\'s audit trail lands here.</div>';
  else body=`<div class="sf-scroll" style="max-height:320px;overflow-y:auto">${rows.map(a=>{
    const sc=a.status==="done"?"var(--green)":a.status==="failed"?"var(--red)":"var(--amber)";
    const ps=Object.entries(a.params||{}).map(([k,v])=>`${k}=${v}`).join(" ")||"–";
    const r=a.result||{}; const rs=a.status==="failed"?(r.error||"–")
      : r.filled!=null?`${r.filled}/${r.submitted} filled${r.overlay_closed?` · spread ×${r.overlay_closed}`:""}`:"–";
    return `<div class="prow" style="${G};padding:9px 16px"><span class="pmono" style="font-size:11px;color:var(--muted)">${(a.ts||'').replace('T',' ').slice(0,19)}</span><span style="font:600 10.5px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:var(--fg-dim)">${a.action}</span><span class="pmono" style="font-size:10.5px;color:${a.mode==="express"?"var(--amber)":"var(--muted)"}">${a.mode||'–'}</span><span class="pmono" style="font-size:11px;color:var(--fg-dim)">${esc(ps)}</span><span style="font:600 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:${sc}">${a.status}</span><span class="pmono" style="font-size:11px;color:var(--fg-dim);overflow:hidden;text-overflow:ellipsis" title="${esc(String(rs))}">${esc(String(rs))}</span></div>`; }).join('')}</div>`;
  fillPanel(panelId||"a-manual","Manual actions",`execution console audit trail <button class="exm-chip" onclick="exportCSV('manual_actions',_manualRows)">⤓ csv</button>`,head+body,"orders");
}

// Solid sector colour for ribbons/swatches (brighter than the chip background).
// Low-saturation, signed-neutral palette so the ring reads as restrained — never competing with
// the teal accent or the green/red reserved for P&L (see "sectors" in the SFI design tokens).
function secColor(sec){ const h=SECTOR_HUE[sec]; return h==null ? "hsl(220 8% 44%)" : `hsl(${h} 34% 47%)`; }
// A ticker's shade within its sector: same hue, lightness stepped so names are distinguishable yet
// clearly belong to the sector's (desaturated) colour family.
function tkrShade(sec, idx, count){
  const h = SECTOR_HUE[sec], t = count>1 ? idx/(count-1) : 0;
  return h==null ? `hsl(220 7% ${(58-t*18).toFixed(0)}%)` : `hsl(${h} 28% ${(63-t*22).toFixed(0)}%)`;
}

// Nested donut: inner ring = sectors, outer ring = tickers grouped under their sector (same start
// angle + ordering ⇒ each ticker wedge sits beneath its sector wedge). Labels are drawn straight
// onto the rings (sector % inside, ticker symbol outside). Items: {share, color, tip, label, over}.
function nestedDonut(inner, outer, centerTop, centerBot){
  const cx=150, cy=150, oR=140, oRi=116, iR=108, iRi=84;     // two thin rings + a large airy hole
  const oMid=(oR+oRi)/2, iMid=(iR+iRi)/2;
  const bg=cssv('--panel','#0e1830'), fg=cssv('--fg','#eaf2fb'), mut=cssv('--muted','#65758c');
  const X=(r,a)=>(cx+r*Math.cos(a)).toFixed(1), Y=(r,a)=>(cy+r*Math.sin(a)).toFixed(1);
  const wedge=(r0,r1,a0,a1,color,tip)=>{ const lo=(a1-a0)>Math.PI?1:0;
    return `<path d="M${X(r0,a0)} ${Y(r0,a0)} A${r0} ${r0} 0 ${lo} 1 ${X(r0,a1)} ${Y(r0,a1)} `+
      `L${X(r1,a1)} ${Y(r1,a1)} A${r1} ${r1} 0 ${lo} 0 ${X(r1,a0)} ${Y(r1,a0)} Z" `+
      `fill="${color}" stroke="${bg}" stroke-width="1.25" data-tip="${esc(tip)}"/>`; };
  // light text with a soft shadow (not a hard outline) so it reads on the muted wedges but stays sleek
  const label=(r,a,txt,size,weight,fill)=>`<text x="${X(r,a)}" y="${Y(r,a)}" text-anchor="middle" `+
    `dominant-baseline="central" font-size="${size}" font-weight="${weight}" fill="${fill}" `+
    `filter="url(#dlbl)" style="pointer-events:none;letter-spacing:.2px">${esc(txt)}</text>`;
  let s=`<svg viewBox="0 0 300 300" width="100%" preserveAspectRatio="xMidYMid meet" style="display:block;max-width:380px;margin:0 auto">`;
  s+=`<defs><filter id="dlbl" x="-25%" y="-25%" width="150%" height="150%"><feDropShadow dx="0" dy="0" stdDeviation="1" flood-color="#05080e" flood-opacity="0.75"/></filter></defs>`;
  let lbl="", a=-Math.PI/2;
  inner.forEach(it=>{ const a1=a+it.share*2*Math.PI, mid=(a+a1)/2; s+=wedge(iR,iRi,a,a1,it.color,it.tip);
    if(it.label && it.share>0.05) lbl+=label(iMid,mid,it.label,11,600,'rgba(255,255,255,.88)'); a=a1; });
  a=-Math.PI/2;
  outer.forEach(it=>{ const a1=a+it.share*2*Math.PI, mid=(a+a1)/2; s+=wedge(oR,oRi,a,a1,it.color,it.tip);
    if(it.label && it.share>0.045) lbl+=label(oMid,mid,it.label,10.5,500,'#fff'); a=a1; });
  s+=lbl;   // labels painted above all wedges
  s+=`<text x="${cx}" y="${cy-3}" text-anchor="middle" font-size="15" font-weight="600" fill="${fg}" style="letter-spacing:.2px">${esc(centerTop)}</text>`;
  s+=`<text x="${cx}" y="${cy+15}" text-anchor="middle" font-size="10.5" fill="${mut}" style="letter-spacing:.4px;text-transform:uppercase">${esc(centerBot)}</text>`;
  return s+"</svg>";
}

function renderSectorExposure(positions){
  const cap = META.max_sector_pct || 0.30;
  const items = (positions||[]).map(p=>{ const sym=String(p.symbol).toUpperCase(), ref=REF[sym]||{};
    return {sym, w:p.weight||0, sec:ref.sector||"Unknown", name:ref.name}; }).filter(p=>p.w>0);
  const total = items.reduce((a,p)=>a+p.w,0);
  if(!items.length || !total){ $("sectors").innerHTML='<div class="se-empty">no positions yet — sector mix appears once the book trades</div>'; $("secsub").textContent=""; return; }
  // group names by sector, sectors sorted by share, names within a sector sorted by weight
  const bySec={}; items.forEach(p=>{ (bySec[p.sec]=bySec[p.sec]||[]).push(p); });
  const sectors = Object.entries(bySec).map(([sec,names])=>({sec, names:names.sort((a,b)=>b.w-a.w),
      share:names.reduce((s,p)=>s+p.w,0)/total})).sort((a,b)=>b.share-a.share);
  $("secsub").textContent = `share of book · sectors over ${(cap*100).toFixed(0)}% cap flagged`;

  // donut wedges (inner = sectors, outer = tickers in sector order so they nest); labels on the rings
  const secWedges = sectors.map(s=>({share:s.share, color:secColor(s.sec),
    label:`${Math.round(s.share*100)}%`,
    tip:`${s.sec} · ${(s.share*100).toFixed(1)}% · ${s.names.length} name${s.names.length>1?'s':''}`}));
  const tkrWedges = []; sectors.forEach(s=> s.names.forEach((p,ti)=> tkrWedges.push({share:p.w/total,
    color:tkrShade(s.sec, ti, s.names.length), label:p.sym,
    tip:`${p.sym}${p.name?' · '+p.name:''} · ${(p.w/total*100).toFixed(1)}%`})));
  const donut = nestedDonut(secWedges, tkrWedges, `${items.length} names`, `${sectors.length} sectors`);

  // slim legend strip (colour → sector name + share, over-cap flagged) under the ring
  const legend = sectors.map(s=>{
    const over = s.share > cap + 1e-9;
    return `<span class="legitem2 ${over?'over':''}"${over?` data-tip="Over the ${(cap*100).toFixed(0)}% sector cap"`:''}>`+
      `<span class="swatch" style="background:${secColor(s.sec)}"></span>${s.sec} <b>${(s.share*100).toFixed(1)}%</b>`+
      `${over?'<span class="capflag">▲</span>':''}</span>`;
  }).join("");

  // concentration footer (share-of-book, leverage-independent)
  const shares = items.map(p=>p.w/total).sort((a,b)=>b-a);
  const topN = k => (shares.slice(0,k).reduce((a,b)=>a+b,0)*100).toFixed(0);
  const hhi = shares.reduce((a,b)=>a+b*b,0), eff = hhi>0 ? 1/hhi : 0;
  const conc = `<div class="conc">`+
    `<span data-tip="Combined weight of the five largest positions (share of book).">Top 5 names <b>${topN(5)}%</b></span>`+
    `<span data-tip="Effective number of equally-weighted names = 1 / Σweightᵢ². Lower than the actual count when a few names dominate.">Effective names <b>${eff.toFixed(1)}</b> <span class="mut">of ${shares.length}</span></span>`+
    `<span data-tip="Largest single position as a share of the book.">Largest <b>${(shares[0]*100).toFixed(1)}%</b></span>`+
    `<span data-tip="Distinct GICS sectors represented in the book.">Sectors <b>${sectors.length}</b></span></div>`;
  $("sectors").innerHTML = `<div class="sec-wrap"><div class="donut-wrap">${donut}</div><div class="sec-legend">${legend}</div>${conc}</div>`;
}

// Factor tilt — the book's share-of-book-weighted exposure to each factor. The stored sub-scores
// are cross-sectional z-scores (universe mean ≈ 0), so a weighted average reads directly as the
// active lean vs the universe: +ve = the book leans into the factor, −ve = leans away.
const FACTOR_KEYS = [
  ["quality","Quality","profitability","Quality — profitability, margins & balance-sheet strength."],
  ["value","Value","cheapness","Value — cheapness on earnings & book multiples."],
  ["beta","Low Beta","market β","Low Beta — trailing-252d β vs SPY; lower (less market-sensitive) scores higher."],
  ["lowvol","Low Vol","stability","Low Vol — lower realized volatility scores higher."]];
function renderFactorTilt(positions, factors){
  const facBy = {}; (factors||[]).forEach(f=>{ facBy[String(f.symbol).toUpperCase()] = f; });
  // weight only the names that carry a factor score; share-of-book over that scored sleeve
  let scoredW = 0; const parts = [];
  (positions||[]).forEach(p=>{ const w=p.weight||0, f=facBy[String(p.symbol).toUpperCase()];
    if(!w || !f) return; scoredW += w; parts.push({w, f}); });
  const equityNames = (positions||[]).filter(p=>p.weight && String(p.symbol).length<=5).length;
  if(!parts.length || !scoredW){
    $("ftilt").innerHTML='<div class="se-empty">factor tilt appears once the book is scored and trades</div>'; $("ftsub").textContent=""; return; }
  $("ftsub").textContent = "book-weighted z-score · 0 = universe average";
  const wavg = key => parts.reduce((a,{w,f})=>a + (w/scoredW)*(f[key]==null?0:f[key]), 0);
  const tilt = {}; FACTOR_KEYS.forEach(([k])=>{ tilt[k]=wavg(k); });
  // composite = equal-weight blend of the four sub-tilts (the engine's composite definition),
  // computed from the displayed tilts so the footer never contradicts the bars
  const comp = FACTOR_KEYS.reduce((a,[k])=>a+tilt[k], 0) / FACTOR_KEYS.length;
  // symmetric domain: ±1σ fills a half-bar unless a tilt is larger (then it scales out)
  const dom = Math.max(1.0, ...FACTOR_KEYS.map(([k])=>Math.abs(tilt[k])));
  const head = `<div class="fthead"><span></span><span class="scale"><span>‹ leans away</span><span>neutral</span><span>leans in ›</span></span><span></span></div>`;
  const rows = FACTOR_KEYS.map(([k,label,desc,tip])=>{
    const v = tilt[k], pos = v>=0, col = pos?"var(--green)":"var(--amber)";
    const half = Math.min(Math.abs(v)/dom, 1)*50;
    const bar = pos ? `left:50%;width:${half.toFixed(1)}%` : `left:${(50-half).toFixed(1)}%;width:${half.toFixed(1)}%`;
    return `<div class="ftrow">
      <div class="ftname" data-tip="${esc(tip+" Book-weighted average z-score across held names.")}">${label} <small>· ${desc}</small></div>
      <div class="ftbar"><span class="axis"></span><i style="${bar};background:${col}"></i></div>
      <div class="ftval" style="color:${col}">${pos?"+":""}${v.toFixed(2)}</div>
    </div>`;
  }).join("");
  const foot = `<div class="conc">`+
    `<span data-tip="Equal-weight blend of the four sub-tilts — the book's overall factor lean vs the universe.">Composite tilt <b style="color:${comp>=0?'var(--green)':'var(--amber)'}">${comp>=0?"+":""}${comp.toFixed(2)}</b></span>`+
    `<span data-tip="Share of the equity book that carries a current factor score and feeds the tilt.">Scored coverage <b>${(100*scoredW/Math.max(1e-9,(positions||[]).filter(p=>p.weight&&String(p.symbol).length<=5).reduce((a,p)=>a+p.weight,0))).toFixed(0)}%</b></span>`+
    `<span data-tip="Held names with a current factor score.">Names scored <b>${parts.length}</b> <span class="mut">of ${equityNames}</span></span></div>`;
  $("ftilt").innerHTML = `<div class="ft-wrap">${head}${rows}${foot}</div>`;
}

// OCC option symbols end in a 6-digit expiry + C/P + 8-digit strike (e.g. AAPL260717C00210000).
function _isOpt(sym){ return /\d{6}[CP]\d{8}$/.test(String(sym||"")); }

// (renderGate/renderPositions/overlayIndicator/renderCalls/renderFactors removed in the
// 2026-07 audit — dead since the Overview/Portfolio overhaul replaced their panels.)

function renderOrders(orders){
  _ordersRows = orders || [];
  const G="display:grid;grid-template-columns:2fr .7fr .7fr 1fr .9fr;gap:10px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Instrument</span><span>Side</span><span style="text-align:right">Qty</span><span>Status</span><span style="text-align:right">Fill px</span></div>`;
  let body;
  if(!(orders&&orders.length)) body='<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">No orders this session.</div>';
  else body=`<div class="sf-scroll" style="max-height:440px;overflow-y:auto">${orders.map(o=>{ const r=REF[(o.symbol||'').toUpperCase()]||{}, sc=o.side==='buy'?'var(--green)':'var(--red)';
    return `<div class="prow" style="${G};padding:8px 16px"><span style="display:flex;align-items:center;gap:10px;min-width:0">${tkrChip(o.symbol,r.sector)}<span style="font:500 12px 'Space Grotesk';color:var(--fg-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.name||o.symbol}</span></span><span style="font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:${sc}">${o.side||'–'}</span><span class="pmono" style="text-align:right;font-size:12px;color:var(--fg-dim)">${fmt(o.qty)}</span><span class="pmono" style="font-size:11px;color:var(--muted)">${o.status||'–'}</span><span class="pmono" style="text-align:right;font-size:12px;color:var(--fg)">${o.filled_avg_price==null?'–':usd2(o.filled_avg_price)}</span></div>`; }).join('')}</div>`;
  fillPanel("a-orders","Recent orders",`limit orders · last session <button class="exm-chip" onclick="exportCSV('orders',_ordersRows)">⤓ csv</button>`,head+body,"orders");
}

// Alerts log — filterable by severity. _alerts holds the last fetch; _alertFilter the active pill.
let _alerts = [], _alertFilter = localStorage.getItem('sepi_alertf') || "all";
const _alertCls = sv => sv==="error" ? "var(--red)" : sv==="warn" ? "var(--amber)" : "var(--muted)";
function setAlertFilter(f){ _alertFilter=f; localStorage.setItem('sepi_alertf', f); drawAlerts(); }
function renderAlerts(alerts){ _alerts = alerts || []; drawAlerts(); }
function drawAlerts(){
  const f=_alertFilter, rows=_alerts.filter(a=>f==="all"||(a.severity||"info")===f);
  const c={error:0,warn:0,info:0}; _alerts.forEach(a=>{ c[a.severity||"info"]=(c[a.severity||"info"]||0)+1; });
  const FIL=[["all","All",_alerts.length],["error","Errors",c.error],["warn","Warnings",c.warn],["info","Info",c.info]];
  const pills=FIL.map(([k,l,n])=>`<button onclick="setAlertFilter('${k}')" class="evtf${_alertFilter===k?' on':''}">${l}${k!=="all"?` <span style="opacity:.55">${n}</span>`:''}</button>`).join('');
  const filterRow=`<div style="display:flex;gap:5px;flex-wrap:wrap;padding:10px 14px 6px;border-bottom:1px solid var(--line-soft)">${pills}</div>`;
  const G="display:grid;grid-template-columns:150px 84px 1fr 70px;gap:12px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Time (UTC)</span><span>Sev</span><span>Message</span><span style="text-align:right">Emailed</span></div>`;
  let body;
  if(!rows.length) body=`<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">No ${f==="all"?"":f+" "}alerts.</div>`;
  else body=`<div class="sf-scroll" style="max-height:440px;overflow-y:auto">${rows.map(a=>`<div class="prow" style="${G};padding:9px 16px"><span class="pmono" style="font-size:11px;color:var(--muted)">${(a.ts||'').replace('T',' ').slice(0,19)}</span><span style="font:500 10px 'Space Grotesk';letter-spacing:.04em;text-transform:uppercase;color:${_alertCls(a.severity)}">${a.severity||'info'}</span><span style="font:400 12px 'Space Grotesk';color:var(--fg-dim);min-width:0;overflow:hidden;text-overflow:ellipsis" title="${esc(a.message||'')}">${a.message||'–'}</span><span class="pmono" style="text-align:right;font-size:11px;color:${a.delivered?'var(--green)':'var(--muted)'}">${a.delivered?'✓ sent':'dry-run'}</span></div>`).join('')}</div>`;
  fillPanel("a-alerts","Alerts",`${_alerts.length} in last 24h · dot = worst severity`,filterRow+head+body,"alerts");
}

function renderFootstrip(s){
  const stale = snapTs!=null && (Date.now()-snapTs)/1000 > 360;
  const dot = ok => `<span class="fdot${ok?'':' off'}"></span>`;
  const item = (k,v,cls) => `<span class="fitem">${k}<span class="fv ${cls||''}">${v}</span></span>`;
  $("footstrip").innerHTML =
    `<span class="fitem">${dot(true)}Postgres</span>` +
    `<span class="fitem">${dot(META.live)}Alpaca<span class="fv">${META.live?`live · ${META.monitor_interval||60}s`:'Postgres-only'}</span></span>` +
    item("Snapshot", `${(s.ts||"–").replace("T"," ").slice(0,19)}Z`, stale?'warn':'') +
    item("Env", (META.env||"paper").toUpperCase()) +
    item("Target Δ", (META.target_delta||0.30).toFixed(2)) +
    `<span class="fnote">${META.live?'self-updating from Alpaca':'monitor bridges Alpaca → snapshots'}</span>`;
}
function humanAge(s){ s=Math.max(0,Math.round(s)); return s<60?s+"s":s<3600?Math.round(s/60)+"m":Math.round(s/3600)+"h"; }

// header status: ticks every second so "checked" counts up (and the ↻/30s poll reset it),
// while "data Nago" shows the snapshot's true age and turns amber when stale (>6 min).
function tickFreshness(){
  if (backendDown){ $("dot").className = "live-dot down"; $("upd").textContent = "backend unreachable"; return; }
  const checked = lastFetch ? (Date.now()-lastFetch)/1000 : null;
  const dataAge = snapTs!=null ? (Date.now()-snapTs)/1000 : null;
  const stale = dataAge!=null && dataAge>360;
  $("dot").className = "live-dot" + (stale?" stale":"");
  const chk = checked==null ? "–" : (checked<5 ? "just now" : humanAge(checked)+" ago");
  const dat = dataAge==null ? "no data yet" : "data "+humanAge(dataAge)+" old";
  $("upd").innerHTML = `checked ${chk} · <span class="${stale?'warn':'mut'}">${dat}</span>`;
}

// ---- boot -----------------------------------------------------------------
async function boot(){
  const [m, ref] = await Promise.all([get("/api/meta"), get("/api/reference")]);
  if(m) META = Object.assign(META, m);
  REF = ref || {};
  const eb = $("envbadge"); const live = (META.env||"paper")==="live";
  eb.textContent = (META.env||"paper").toUpperCase();
  eb.className = "se-pill " + (live ? "bad" : "warn");
  eb.title = live ? "LIVE trading — real money" : "paper trading — simulated";
  seedSparklines();          // hydrate row sparklines with an intraday window (live ticks extend it)
  loadQuotes();              // quote detail for the ticker hover-card
  loadActive();
}
$("refreshbtn").onclick = () => { $("refreshbtn").classList.add("spin"); setTimeout(()=>$("refreshbtn").classList.remove("spin"),700); loadActive(); };
// ===================== Execution console (the Execute tab) =====================
// Four actions (rebalance / liquidate / leverage / single name), two modes (normal =
// tiered limit chase, express = market orders). The ORDER PLAN panel computes live as
// inputs change — nothing is sent until EXECUTE, which is token-gated server-side.
const EXEC = { tab: "rebalance", mode: "normal", side: "buy", plan: null, planQS: "",
               debounce: null, statusPoll: null, prefill: null };

function execToken(force){
  let t = localStorage.getItem("sepi_exec_token") || "";
  if (!t || force){
    t = (prompt("Execution token (the SEPI_EXEC_TOKEN value from the VM's env file):", "") || "").trim();
    if (t) localStorage.setItem("sepi_exec_token", t);
  }
  return t;
}
function execTokenChange(){ execToken(true); refreshExecStatus(); }
function execTokenClear(){ localStorage.removeItem("sepi_exec_token"); refreshExecStatus(); }

// Lightweight toasts — run outcomes must be visible even after navigating away mid-run.
function toast(msg, bad){
  let h = $("toasts");
  if (!h){ h = document.createElement("div"); h.id = "toasts"; document.body.appendChild(h); }
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  t.onclick = () => t.remove();
  h.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 7000);
}
const _TITLE0 = document.title;
function setRunTitle(action){ document.title = action ? `● ${action} — SEPI` : _TITLE0; }
async function postExec(path, tok){
  try { const r = await fetch(path, { method: "POST", headers: tok ? { "X-Exec-Token": tok } : {} });
        return r.ok ? await r.json() : null; } catch { return null; }
}
function execParams(){
  const p = {};
  if (EXEC.tab === "liquidate") p.pct = parseFloat(($("xPct")||{}).value);
  if (EXEC.tab === "leverage") p.target = parseFloat(($("xTarget")||{}).value);
  if (EXEC.tab === "trade"){
    p.symbol = (($("xSym")||{}).value || "").trim().toUpperCase();
    p.side = EXEC.side;
    if (EXEC.side === "buy") p.usd = parseFloat(($("xUsd")||{}).value);
    else p.pct = parseFloat(($("xSellPct")||{}).value);
  }
  return p;
}
function execQS(extra){
  const p = { action: EXEC.tab, ...execParams(), ...(extra||{}) };
  return Object.entries(p).filter(([,v]) => v!=null && v!=="" && !(typeof v==="number" && isNaN(v)))
    .map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join("&");
}

// Entry from the Portfolio B/S row buttons (and anywhere else): jump to the tab, prefilled.
function openTicket(tab, prefill){
  EXEC.tab = tab || EXEC.tab; EXEC.prefill = prefill || null; EXEC.plan = null; EXEC.planQS = "";
  if (prefill && prefill.side) EXEC.side = prefill.side;
  const t = document.querySelector('.tab[data-view="execute"]');
  if (t) t.click();
}

async function loadExecute(){
  if (!_ovS){ const s = await get("/api/state"); if (s) _ovS = s; }   // held names for the datalist
  renderTicketPanel();
  refreshExecStatus();
  pollExec();                                  // the live run panel lives on this tab now
  renderManualActions(await get("/api/manual_actions"), "x-history");
  renderConfigPanel(await get("/api/config"));
  schedulePlan(0);
}

function setExecAction(a){ EXEC.tab = a; EXEC.plan = null; EXEC.planQS = ""; renderTicketPanel(); schedulePlan(0); }
function setExecMode(m){ EXEC.mode = m; renderTicketPanel(); }
function setExecSide(s){ EXEC.side = s; EXEC.plan = null; EXEC.planQS = ""; renderTicketPanel(); schedulePlan(0); }
function setChipVal(id, v){ const el = $(id); if (el){ el.value = v; } EXEC.plan = null; EXEC.planQS = ""; renderTicketPanel(); schedulePlan(0); }

const _XACTIONS = [
  ["rebalance", "Rebalance", "full engine cycle — targets, spread, equities",
   "Runs one full engine cycle: reconcile → fresh targets (ingest + optimizer) → risk gate → close spread → equity trades → write spread → snapshot. Targets are computed at run time, so there is no pre-trade plan — the run panel above narrates as it executes."],
  ["liquidate", "Liquidate", "sell a % of every position, trim the spread",
   "Sells the chosen % of every equity position pro-rata and trims the SPY spread by the same fraction (100% closes it, short leg first). Proceeds sit in cash until the next rebalance re-invests."],
  ["leverage", "Leverage", "scale the book to a target gross — sticky",
   "Scales every position so gross exposure = target × equity (capped at max_leverage). STICKY: the target becomes the standing leverage parameter for every future rebalance until cleared in Console status. Lever-down also trims the SPY spread proportionally; lever-up leaves the spread for the next overlay pass."],
  ["trade", "Single name", "buy $ or sell % of one stock",
   "Buy a dollar amount (sized at the last price) or sell a % of the position (100% exits). A buy outside the current book is flagged off-model in the plan — the next rebalance will likely unwind it."],
];
const _MODE_TIP = "NORMAL: the engine's execution algos — patient limit ladders walking from mid "
  + "toward the touch, live quotes required, so it only runs during market hours. EXPRESS: market "
  + "orders sent immediately, any time — off-hours orders queue at the broker and fill at the next "
  + "open (they are left working, never cancelled).";

function renderTicketPanel(){
  const host = $("x-ticket"); if (!host) return;
  const pre = EXEC.prefill || {};
  const held = ((_ovS && _ovS.positions) || []).filter(p => p.symbol && !_isOpt(p.symbol) && p.qty);
  const cur = { pct: ($("xPct")||{}).value || pre.pct || 25,
                target: ($("xTarget")||{}).value || pre.target || (META.target_leverage || 2.0),
                sym: ($("xSym")||{}).value || pre.symbol || "",
                usd: ($("xUsd")||{}).value || pre.usd || 5000,
                spct: ($("xSellPct")||{}).value || pre.pct || 100 };
  const acts = _XACTIONS.map(([k, l, d, tip]) => `<button class="xact${EXEC.tab===k?" on":""}" onclick="setExecAction('${k}')" data-tip="${esc(tip)}">
      <span class="xact-l">${l}</span><span class="xact-d">${d}</span></button>`).join("");
  let form = "";
  if (EXEC.tab === "liquidate"){
    form = `<div class="exm-row"><span class="exm-lbl">Sell</span>
      ${[10,25,50,100].map(v => `<button class="exm-chip${+cur.pct===v?" on":""}" onclick="setChipVal('xPct',${v})">${v}%</button>`).join("")}
      <input id="xPct" class="exm-in" type="number" min="1" max="100" step="1" value="${cur.pct}" oninput="EXEC.plan=null;schedulePlan()">
      <span class="exm-lbl">% of every position <span class="srcinfo" data-tip="Pro-rata: each position sells the same %. The SPY spread is trimmed by the same fraction, both legs (100% closes it, short leg first). Proceeds sit in cash until the next rebalance re-invests.">ⓘ</span></span></div>`;
  } else if (EXEC.tab === "leverage"){
    form = `<div class="exm-row"><span class="exm-lbl">Target gross</span>
      <input id="xTarget" class="exm-in" type="number" min="0.1" max="${META.leverage_cap||2}" step="0.05" value="${cur.target}" oninput="EXEC.plan=null;schedulePlan()">
      <span class="exm-lbl">× equity · cap ${(META.leverage_cap||2).toFixed(2)}× <span class="srcinfo" data-tip="STICKY: this target persists as the standing leverage parameter — every future rebalance sizes to it until you clear the override in Console status. Lever-down trims the SPY spread proportionally; lever-up leaves it for the next overlay pass.">ⓘ</span></span></div>`;
  } else if (EXEC.tab === "trade"){
    form = `<div class="exm-row">
      <span class="xseg">${["buy","sell"].map(s => `<button class="xseg-b${EXEC.side===s?" on":""}" onclick="setExecSide('${s}')">${s.toUpperCase()}</button>`).join("")}</span>
      <input id="xSym" class="exm-in wide" list="xHeld" value="${cur.sym}" placeholder="symbol" oninput="EXEC.plan=null;schedulePlan()">
      <datalist id="xHeld">${held.map(p => `<option value="${p.symbol}">`).join("")}</datalist></div>
      ${EXEC.side === "buy"
        ? `<div class="exm-row"><span class="exm-lbl">Buy</span><span class="exm-lbl">$</span>
           <input id="xUsd" class="exm-in wide" type="number" min="1" step="500" value="${cur.usd}" oninput="EXEC.plan=null;schedulePlan()">
           <span class="srcinfo" data-tip="Dollar notional, converted to whole shares at the last price. A name outside the current book is flagged off-model in the plan — the next rebalance will likely unwind it.">ⓘ</span></div>`
        : `<div class="exm-row"><span class="exm-lbl">Sell</span>
           ${[25,50,100].map(v => `<button class="exm-chip${+cur.spct===v?" on":""}" onclick="setChipVal('xSellPct',${v})">${v}%</button>`).join("")}
           <input id="xSellPct" class="exm-in" type="number" min="1" max="100" step="1" value="${cur.spct}" oninput="EXEC.plan=null;schedulePlan()">
           <span class="exm-lbl">% of the position <span class="srcinfo" data-tip="Percent of the shares currently held — 100% exits the name entirely.">ⓘ</span></span></div>`}`;
  }
  const modes = `<div class="exm-row" style="margin-top:2px"><span class="exm-lbl">Mode</span>
    <span class="xseg">${[["normal","NORMAL"],["express","EXPRESS"]].map(([m,l]) =>
      `<button class="xseg-b${EXEC.mode===m?" on":""}" onclick="setExecMode('${m}')">${l}</button>`).join("")}</span>
    <span class="srcinfo" data-tip="${esc(_MODE_TIP)}">ⓘ</span></div>`;
  host.innerHTML = `<div class="ovhead"><span class="ovhk">Ticket</span><span class="ovhs">choose an action · the plan updates live</span></div>
    <div style="padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div class="xacts">${acts}</div>${form}${modes}
      <div style="display:flex;align-items:center;gap:10px;margin-top:4px">
        <button class="exm-btn go" id="xGo" style="flex:1" disabled>Execute</button>
      </div>
      <div class="exm-msg" id="xMsg" style="white-space:normal"></div>
    </div>`;
  $("xGo").onclick = execRun;
  armGo();
}

function armGo(){
  const b = $("xGo"); if (!b) return;
  const running = EXEC.statusPoll != null;
  const ov = EXEC.plan && EXEC.plan.overlay || {};
  b.disabled = running || !(EXEC.tab === "rebalance"
                            || (EXEC.plan && !EXEC.plan.error && ((EXEC.plan.orders||[]).length || ov.close_contracts)));
  b.textContent = running ? "Running…" : "Execute";
}

function schedulePlan(ms){
  clearTimeout(EXEC.debounce);
  EXEC.debounce = setTimeout(execPlanNow, ms == null ? 350 : ms);
}

async function execPlanNow(){
  const P = $("x-plan"); if (!P || activeView !== "execute") return;
  const paint = inner => { P.innerHTML = `<div class="ovhead"><span class="ovhk">Order plan</span>
    <span class="ovhs">computed live — nothing is sent until Execute</span></div>
    <div style="padding:12px 16px">${inner}</div>`; };
  if (EXEC.tab === "rebalance"){
    paint(`<div class="exm-note">No pre-trade plan for the rebalance — the engine computes targets when
      it runs (fresh ingest → optimizer). Execute is armed directly; the run panel above narrates the
      plan as it executes.${EXEC.mode === "express" ? "<br><span class='exm-warn'>▲ express: every equity leg goes out as a market order</span>" : ""}</div>`);
    EXEC.plan = null; armGo(); return;
  }
  const qs = execQS();
  if (qs === EXEC.planQS && EXEC.plan){ armGo(); return; }        // inputs unchanged — keep the plan
  const p = execParams();
  if ((EXEC.tab === "trade" && !p.symbol) || (EXEC.tab === "liquidate" && !(p.pct > 0))
      || (EXEC.tab === "leverage" && !(p.target > 0))){
    paint('<div class="exm-note dim">fill in the ticket — the plan appears here</div>');
    EXEC.plan = null; armGo(); return;
  }
  paint('<div class="exm-note dim">planning…</div>');
  const plan = await get(`/api/exec/preview?${qs}`);
  if (qs !== execQS()) return;                                    // stale response — a newer edit won
  if (!plan){ paint('<div class="exm-err">backend unreachable</div>'); EXEC.plan = null; armGo(); return; }
  if (plan.error){ paint(`<div class="exm-err">${esc(plan.error)}</div>`); EXEC.plan = plan; armGo(); return; }
  EXEC.plan = plan; EXEC.planQS = qs;
  const rows = (plan.orders||[]).map(o => `<div class="exm-prow">
      <span class="pmono sym">${o.symbol}</span>
      <span class="side ${o.side}">${o.side.toUpperCase()}</span>
      <span class="pmono" style="text-align:right">${fmt(o.qty)}</span>
      <span class="pmono" style="text-align:right">${o.est_price==null?"–":usd2(o.est_price)}</span>
      <span class="pmono" style="text-align:right">${o.est_notional==null?"–":usd(o.est_notional)}</span></div>`).join("");
  const ov = plan.overlay || {};
  const ovRow = ov.close_contracts ? `<div class="exm-prow"><span class="pmono sym">${ov.market} spread</span>
      <span class="side sell">CLOSE</span><span class="pmono" style="text-align:right">${ov.close_contracts}/${ov.contracts}</span>
      <span></span><span class="pmono" style="text-align:right">both legs</span></div>` : "";
  const t = plan.totals || {};
  const traded=(t.sell_notional||0)+(t.buy_notional||0), _nav=_ovS&&_ovS.nav;
  const tot = [t.sell_notional ? `raise ~${usd(t.sell_notional)}` : null,
               t.buy_notional ? `deploy ~${usd(t.buy_notional)}` : null,
               (_nav&&traded) ? `${(traded/_nav*100).toFixed(1)}% of NAV` : null,
               t.current_leverage != null ? `${t.current_leverage.toFixed(2)}× → ${(+t.target_leverage).toFixed(2)}×` : null]
              .filter(Boolean).join(" · ");
  const warns = (plan.warnings||[]).map(w => `<div class="exm-warn">▲ ${esc(w)}</div>`).join("");
  const skips = (plan.skipped||[]).length ? `<div class="exm-note dim">${plan.skipped.length} name(s) skipped (too small)</div>` : "";
  paint(`<div class="exm-phead"><span>${(plan.orders||[]).length} order(s)${ovRow?" + spread":""}</span><span>${tot}</span></div>
    <div class="exm-plist">${ovRow}${rows || '<div class="exm-note dim">no equity orders</div>'}</div>${warns}${skips}`);
  armGo();
}

async function execRun(){
  const b = $("xGo"); if (!b || b.disabled) return;
  const p0 = execParams();
  if (EXEC.tab === "liquidate" && p0.pct >= 100){          // one confirm() is thin for "sell everything"
    const typed = (prompt("This sells the ENTIRE book and closes the SPY spread.\nType SELL ALL to confirm:") || "").trim().toUpperCase();
    if (typed !== "SELL ALL"){ $("xMsg").textContent = "cancelled — confirmation text did not match"; return; }
  }
  const tok = execToken(); if (!tok){ $("xMsg").textContent = "no token — cancelled"; return; }
  b.disabled = true; $("xMsg").textContent = "starting…";
  let r = await postExec(`/api/exec/run?${execQS({ mode: EXEC.mode })}`, tok);
  if (r && r.unauthorized){                                       // stale token → reprompt once
    const t2 = execToken(true);
    if (t2) r = await postExec(`/api/exec/run?${execQS({ mode: EXEC.mode })}`, t2);
  }
  if (!r){ $("xMsg").textContent = "failed — backend unreachable"; armGo(); return; }
  if (!r.started){
    $("xMsg").textContent = r.market_closed
      ? `market closed — next open ${(r.next_open||"").replace("T"," ").slice(0,16)} ET (EXPRESS trades any time)`
      : (r.error || "refused");
    armGo(); return;
  }
  $("xMsg").textContent = `running (${r.cycle_key}) — the run panel above narrates it…`;
  setRunTitle(EXEC.tab);
  if (EXEC.statusPoll) clearInterval(EXEC.statusPoll);
  EXEC.statusPoll = setInterval(async () => {
    const st = await get("/api/exec/status");
    if (!st || st.running) return;
    clearInterval(EXEC.statusPoll); EXEC.statusPoll = null;
    setRunTitle(null);
    const oc = st.outcome || {};
    const ok = st.returncode === 0;
    const msg = ok
      ? (oc.summary ? oc.summary : `done — ${oc.filled!=null ? `${oc.filled}/${oc.submitted} filled` : "complete"}${oc.overlay_closed ? `, spread ×${oc.overlay_closed}` : ""}`)
      : `failed — ${oc.error || "see " + (st.log||"log")}`;
    $("xMsg").textContent = msg;
    toast(`${st.action||"run"}: ${msg}`, !ok);
    EXEC.plan = null; EXEC.planQS = "";
    armGo(); refreshExecStatus(); schedulePlan(0);
    renderManualActions(await get("/api/manual_actions"), "x-history");
    loadGlobal();
  }, 3000);
  armGo(); refreshExecStatus();
}

async function execCancelAll(){
  if (!confirm("Cancel ALL working orders?")) return;
  const tok = execToken(); if (!tok) return;
  const r = await postExec("/api/exec/cancel_all", tok);
  $("xMsg").textContent = r ? (r.error || `cancelled ${r.cancelled} order(s)`) : "cancel failed";
  refreshExecStatus();
}

async function clearLevOverride(){
  const tok = execToken(); if (!tok) return;
  const r = await postExec("/api/exec/clear_override", tok);
  $("xMsg").textContent = r && r.cleared ? "override cleared — rebalances return to settings.yaml" : (r && r.error || "failed");
  refreshExecStatus(); if (EXEC.tab === "leverage") schedulePlan(0);
}

async function refreshExecStatus(){
  const host = $("x-status"); if (!host) return;
  const st = await get("/api/exec/status");
  const line = (k, v) => `<div style="display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line-soft)">
    <span class="exm-lbl">${k}</span><span style="font:500 12px var(--mono);color:var(--fg-dim);text-align:right;min-width:0">${v}</span></div>`;
  let inner;
  if (!st){ inner = '<div class="exm-err">status unreachable</div>'; }
  else {
    const tokLn = st.token_configured
      ? '<span style="color:var(--green)">configured</span>'
      : '<span style="color:var(--amber)">not set — console disabled until SEPI_EXEC_TOKEN is in the VM env</span>';
    const lev = st.leverage_override != null
      ? `<span style="color:var(--accent)">${(+st.leverage_override).toFixed(2)}×</span> override since ${(st.leverage_override_since||"").slice(0,16).replace("T"," ")}
         <button class="exm-chip" onclick="clearLevOverride()">clear</button>`
      : `${st.settings_target_leverage != null ? (+st.settings_target_leverage).toFixed(2)+"×" : "–"} from settings.yaml`;
    const run = st.running ? `<span style="color:var(--amber)">${st.action} (${st.mode}) — ${st.cycle_key}</span>` : "idle";
    const btok = localStorage.getItem("sepi_exec_token")
      ? `saved in this browser <button class="exm-chip" onclick="execTokenChange()">change</button> <button class="exm-chip" onclick="execTokenClear()">clear</button>`
      : `not set <button class="exm-chip" onclick="execTokenChange()">set</button>`;
    const cash = (_ovS && _ovS.nav != null)
      ? `${usd(_ovS.cash)} cash · ${_ovS.gross_exposure!=null?usd(_ovS.gross_exposure)+" gross · ":""}${_ovS.leverage!=null?_ovS.leverage.toFixed(2)+"×":""}`
      : "–";
    const bp = st.buying_power != null
      ? `${usd(st.buying_power)} BP${st.maintenance_margin!=null?` · ${usd(st.maintenance_margin)} maint. margin`:""}`
      : "–";
    inner = line("Server token", tokLn) + line("Browser token", btok)
          + line("Standing leverage target", lev) + line("Cash / exposure", cash)
          + line("Buying power", bp) + line("Manual run", run);
  }
  host.innerHTML = `<div class="ovhead"><span class="ovhk">Console status</span>
      <span class="ovhs"><button class="exm-btn" onclick="execCancelAll()" style="padding:5px 10px">Cancel all orders</button></span></div>
    <div style="padding:6px 16px 12px">${inner}</div>`;
}
// ---- logo intro: dot grid scales in → wordmark → contracts to SFI → reveals dashboard ----
function playIntro(){
  const ov=$("introOverlay"); if(!ov) return;
  // Charming once a day; friction the other twenty times.
  const today=new Date().toISOString().slice(0,10);
  if(localStorage.getItem("sepi_intro")===today){ ov.remove(); return; }
  localStorage.setItem("sepi_intro", today);
  let h=""; for(let r=0;r<3;r++){ h+='<div class="irow">';
    for(let c=0;c<3;c++) h+=`<span class="dot${c===1?' teal':''}" style="transition-delay:${(r+c)*75}ms"></span>`;
    h+='</div>'; }
  $("introGrid").innerHTML=h;
  const done=()=>{ ov.classList.add('done'); setTimeout(()=>ov.remove(),650); };
  ov.addEventListener('click', done);
  requestAnimationFrame(()=>ov.classList.add('p1'));
  setTimeout(()=>ov.classList.add('p2'), 900);
  setTimeout(()=>ov.classList.add('p3'), 2050);
  setTimeout(done, 3400);
}
playIntro();
boot();

// ---- live ticks (one governed loop — audit W1) -----------------------------
// A single 1s master tick replaces five free-running intervals, governed by visibility and
// session: a HIDDEN tab polls nothing (background tabs used to hammer /api/state every second
// all night), and a fully-CLOSED market (overnight/weekends — pre/post stay live per the
// extended-hours design) slows the state tick 1s→15s and the execution poll 2.5s→60s. While
// you're actually watching during any session, the cadence is identical to before.
const _CAD = { state: 1000, stateClosed: 15000, exec: 2500, execClosed: 60000,
               active: 30000, quotes: 45000 };
const _lastPoll = { state: 0, exec: 0, active: Date.now(), quotes: Date.now() };
async function _governedTick(){
  if (document.hidden) return;                       // W1: a background tab costs nothing
  tickFreshness(); tickSession();                    // cheap local paints — only when visible
  const closed = mktSession() === 'closed';
  const now = Date.now();
  if ((activeView === "overview" || activeView === "portfolio")
      && now - _lastPoll.state >= (closed ? _CAD.stateClosed : _CAD.state)) {
    _lastPoll.state = now;
    const s = await get("/api/state");
    if (s) { noteFresh(s);
      if (activeView === "overview") paintOverview(s);   // live repaint (NAV flash, treemap, gauge…)
      else paintPortfolioLive(s); }                      // positions + calls + tape live prices
  }
  if (activeView === "execute" && now - _lastPoll.exec >= (closed ? _CAD.execClosed : _CAD.exec)) {
    _lastPoll.exec = now; pollExec();
  }
  if (now - _lastPoll.active >= _CAD.active) { _lastPoll.active = now; loadActive(); }
  if (now - _lastPoll.quotes >= _CAD.quotes) { _lastPoll.quotes = now; loadQuotes(); }
}
setInterval(_governedTick, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {                            // instant catch-up the moment you come back
    _lastPoll.state = _lastPoll.exec = 0;
    _lastPoll.active = Date.now();
    loadActive();
  }
});
tickSession();                    // paint the ET clock immediately on boot

// ===================== Review-pass C4 additions =====================
// Premium ledger — the REALIZED answer to "what premium yield am I earning".
function renderPremiumLedger(pl){
  const host=$("pr-premium"); if(!host) return;
  if(!pl || !pl.available){ fillPanel("pr-premium","Premium ledger","realized option income by month",'<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">No option premium booked yet — the first spread write starts this ledger.</div>',"overlay"); return; }
  const kc=(l,v,c)=>`<div style="background:var(--panel);padding:12px 16px"><div style="font:600 10px 'Space Grotesk';letter-spacing:.08em;text-transform:uppercase;color:var(--muted)">${l}</div><div style="font:500 19px var(--mono);margin-top:6px;color:${c||'var(--fg)'}">${v}</div></div>`;
  const kpis=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line-soft)">${
    kc("Collected", usd(pl.collected))}${kc("Paid back", usd(pl.paid), pl.paid?'var(--red)':'var(--fg)')}${
    kc("Net kept", usd(pl.net), pl.net>=0?'var(--green)':'var(--red)')}${
    kc("Capture", pl.capture==null?'—':(pl.capture*100).toFixed(0)+'%')}</div>`;
  const mx=Math.max(1,...pl.months.map(m=>Math.max(m.collected,m.paid)));
  const head=`<div style="display:grid;grid-template-columns:70px 1fr 1fr 90px;gap:10px;padding:10px 0 4px;font:600 10px 'Space Grotesk';letter-spacing:.06em;text-transform:uppercase;color:var(--muted)"><span>Month</span><span>Collected</span><span>Paid back</span><span style="text-align:right">Net</span></div>`;
  const rows=pl.months.map(m=>`<div style="display:grid;grid-template-columns:70px 1fr 1fr 90px;gap:10px;align-items:center;padding:6px 0">
    <span class="pmono" style="font-size:11px;color:var(--fg-dim)">${m.month}</span>
    <span style="height:9px;border-radius:4px;background:#0c1626;overflow:hidden"><span style="display:block;height:100%;width:${(m.collected/mx*100).toFixed(1)}%;background:var(--accent)"></span></span>
    <span style="height:9px;border-radius:4px;background:#0c1626;overflow:hidden"><span style="display:block;height:100%;width:${(m.paid/mx*100).toFixed(1)}%;background:var(--red);opacity:.8"></span></span>
    <span class="pmono" style="text-align:right;font-size:12px;color:${m.net>=0?'var(--green)':'var(--red)'}">${(m.net>=0?'+':'-')+'$'+fmt(Math.abs(m.net),0)}</span></div>`).join("");
  fillPanel("pr-premium","Premium ledger",`realized option income · lifetime net ${usd(pl.net)}`,kpis+`<div style="padding:4px 16px 12px">${head}${rows}</div>`,"overlay");
}

// TCA — express vs normal becomes a measured number instead of a debate.
function renderTCA(t){
  const host=$("ex-tca"); if(!host) return;
  if(!t || !t.available){ fillPanel("ex-tca","Execution cost analysis","per cycle · normal vs express",'<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">Appears after the first cycle writes chase telemetry.</div>',"orders"); return; }
  const G="display:grid;grid-template-columns:1.4fr 90px 70px 90px 100px 120px;gap:10px;align-items:center";
  const head=`<div style="${G};padding:8px 16px;font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line-soft)"><span>Cycle</span><span>Style</span><span style="text-align:right">Names</span><span style="text-align:right">Filled</span><span style="text-align:right">Avg rounds</span><span style="text-align:right">Avg bps vs mid</span></div>`;
  const body=t.cycles.map(c=>`<div class="prow" style="${G};padding:8px 16px">
    <span class="pmono" style="font-size:11px;color:var(--fg-dim);overflow:hidden;text-overflow:ellipsis">${c.cycle}</span>
    <span style="font:600 10px 'Space Grotesk';letter-spacing:.05em;text-transform:uppercase;color:${c.style==='express'?'var(--amber)':'var(--accent)'}">${c.style}</span>
    <span class="pmono" style="text-align:right;font-size:12px">${c.names}</span>
    <span class="pmono" style="text-align:right;font-size:12px">${c.filled}/${c.names}</span>
    <span class="pmono" style="text-align:right;font-size:12px">${c.avg_rounds==null?'—':c.avg_rounds}</span>
    <span class="pmono" style="text-align:right;font-size:12px;color:${c.avg_bps==null?'var(--muted)':c.avg_bps>0?'var(--red)':'var(--green)'}">${c.avg_bps==null?'—':(c.avg_bps>0?'+':'')+c.avg_bps}</span></div>`).join("");
  fillPanel("ex-tca","Execution cost analysis","per cycle · +bps = paid up vs the first-post mid",head+body,"orders");
}

// Rolling realized β vs SPY — the live check on the low-beta thesis (and the future
// multiplier for a beta-matched benchmark line).
function renderRollingBeta(risk){
  const host=$("rk-beta"); if(!host) return;
  const rb=(risk&&risk.rolling_beta)||[];
  const pts=rb.filter(v=>v!=null);
  if(!pts.length){ fillPanel("rk-beta","Rolling realized β vs SPY","20-day window",'<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">Appears after ~4 weeks of live curve (a 20-day rolling window needs to fill).</div>',"navcurve"); return; }
  const last=pts[pts.length-1];
  const W=1160,H=140,pl2=44,pr2=12,pt2=10,pb2=14,pw=W-pl2-pr2,ph=H-pt2-pb2;
  let lo=Math.min(0,...pts),hi=Math.max(1,...pts); const pad=(hi-lo)*.15||.1; lo-=pad; hi+=pad;
  const n=rb.length,X=i=>pl2+(n<=1?0:i/(n-1)*pw),Y=v=>pt2+(hi-v)/(hi-lo)*ph;
  const grid=cssv('--grid','#16223a'),mut=cssv('--muted','#65758c'),acc=cssv('--accent','#46b8ad');
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block;height:140px">`;
  [0,0.5,1].forEach(v=>{ if(v>lo&&v<hi){ const y=Y(v).toFixed(1);
    s+=`<line x1="${pl2}" y1="${y}" x2="${W-pr2}" y2="${y}" stroke="${grid}"${v===1?' stroke-dasharray="4 3"':''}/><text x="${pl2-6}" y="${(+y+3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="${mut}" font-family="IBM Plex Mono">${v.toFixed(1)}</text>`; }});
  let d="",started=false;
  rb.forEach((v,i)=>{ if(v==null) return; d+=(started?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1); started=true; });
  s+=`<path d="${d}" fill="none" stroke="${acc}" stroke-width="1.8" stroke-linejoin="round"/></svg>`;
  fillPanel("rk-beta","Rolling realized β vs SPY",`20-day window · now ${last.toFixed(2)} · live check on the low-beta thesis`,`<div style="padding:8px 10px 10px">${s}</div>`,"navcurve");
}

// Live parameters — which wing/delta/coverage/leverage the engine runs NOW, without SSH.
function renderConfigPanel(cfg){
  const host=$("x-config"); if(!host) return;
  if(!cfg || !cfg.available){ fillPanel("x-config","Live parameters","settings.yaml as the engine runs it",'<div style="padding:16px;font:400 11.5px var(--mono);color:var(--muted)">settings unavailable</div>',"clock"); return; }
  const sec=(title,obj)=>`<div><div style="font:600 10px 'Space Grotesk';letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">${title}</div>${
    Object.entries(obj||{}).map(([k,v])=>`<div style="display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-top:1px solid var(--line-soft)"><span style="font:400 11.5px 'Space Grotesk';color:var(--fg-dim)">${k}</span><span class="pmono" style="font-size:11.5px;color:var(--fg)">${v}</span></div>`).join("")}</div>`;
  fillPanel("x-config","Live parameters","what the engine runs right now · read-only (edit settings.yaml + deploy to change)",
    `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px 26px;padding:12px 16px">${sec("Portfolio",cfg.portfolio)}${sec("SPY overlay",cfg.covered_calls)}${sec("Execution",cfg.execution)}</div>`,"clock");
}

// CSV exports — client-side, from whatever the panel already loaded.
let _manualRows=[], _ordersRows=[];
function exportCSV(name, rows){
  if(!rows || !rows.length){ toast("nothing to export", true); return; }
  const keys=Object.keys(rows[0]);
  const cell=v=>{ if(v==null) v=""; if(typeof v==="object") v=JSON.stringify(v);
    v=String(v).replace(/"/g,'""'); return /[",\n]/.test(v)?`"${v}"`:v; };
  const csv=[keys.join(",")].concat(rows.map(r=>keys.map(k=>cell(r[k])).join(","))).join("\n");
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download=`sepi_${name}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click(); URL.revokeObjectURL(a.href);
}
function exportTrackCSV(){
  if(!_perfTR || !_perfTR.dates || !_perfTR.dates.length){ toast("no track record yet", true); return; }
  exportCSV("track_record", _perfTR.dates.map((d,i)=>({date:d, nav:_perfTR.nav[i], norm:_perfTR.norm[i]})));
}
