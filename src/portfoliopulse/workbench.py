"""The analyst workbench page, served at / and /app.

Design notes: a diligence-file identity — claims are exhibits with serif
quotes and numbered footnote provenance, hypotheses are dashed-border
interpretations, decisions render as stamps. The hero is a live SVG
"research assembly line" whose counts come straight from run data.
Vanilla HTML/CSS/SVG on purpose: zero build steps, zero extra deps.
"""

_APP_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PortfolioPulse workbench</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:ital,wght@0,600;0,700;1,400&display=swap" rel="stylesheet">
<style>
:root{
 --carbon:#1E2A38; --muted:#5A6B7E; --paper:#F4F6F9; --sheet:#FFFFFF;
 --rule:#D8DEE7; --rule-strong:#B9C3D1;
 --signal:#2C5FA8; --verify:#1F6F54; --ledger:#A63A2B; --flag:#8F6414;
 --tint-ok:#E7F2EC; --tint-bad:#F7E9E6; --tint-flag:#F5EEDD; --tint-info:#E8EFF9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--carbon);
 font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:15px;line-height:1.5}
main{max-width:68rem;margin:0 auto;padding:0 1.25rem 4rem}
a{color:var(--signal);text-decoration-color:var(--rule-strong);text-underline-offset:2px}
a:hover{text-decoration-color:currentColor}
h1,h2{font-family:'IBM Plex Serif',Georgia,serif;line-height:1.2;margin:0}
h2{font-size:1.15rem;margin:1.6rem 0 .55rem}
button{font:inherit;padding:.42rem .8rem;cursor:pointer;background:var(--sheet);
 border:1px solid var(--rule-strong);border-radius:4px;color:var(--carbon)}
button:hover{border-color:var(--carbon)}
button.primary{background:var(--signal);border-color:var(--signal);color:#fff}
button.primary:hover{background:#24508D}
button.danger{color:var(--ledger);border-color:currentColor}
button:focus-visible,a:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
input,textarea{font:inherit;width:100%;padding:.42rem .55rem;border:1px solid var(--rule-strong);
 border-radius:4px;background:var(--sheet);color:var(--carbon)}
textarea{min-height:4.4rem;resize:vertical}
label{display:block;margin:.55rem 0 .15rem;font-size:.86rem;font-weight:500}
.meta{color:var(--muted);font-size:.84rem}
.panel{background:var(--sheet);border:1px solid var(--rule);border-radius:6px;padding:1rem 1.15rem}
/* header */
header.site{display:flex;flex-wrap:wrap;align-items:baseline;gap:.9rem;
 padding:1.4rem 0 1.1rem;border-bottom:1px solid var(--rule);margin-bottom:1.3rem}
.wordmark{font-family:'IBM Plex Serif',Georgia,serif;font-weight:700;font-size:1.55rem}
.tagline{color:var(--muted);font-size:.92rem}
.chip{display:inline-block;padding:.08rem .5rem;border-radius:11px;font-size:.78rem;font-weight:500;
 border:1px solid var(--rule);background:var(--paper);color:var(--muted)}
.chip.ok{background:var(--tint-ok);color:var(--verify);border-color:var(--verify)}
.chip.bad{background:var(--tint-bad);color:var(--ledger);border-color:var(--ledger)}
.chip.flag{background:var(--tint-flag);color:var(--flag);border-color:var(--flag)}
.chip.info{background:var(--tint-info);color:var(--signal);border-color:var(--signal)}
.stamp{display:inline-block;padding:.05rem .45rem;border:2px solid currentColor;border-radius:3px;
 font-family:'IBM Plex Serif',Georgia,serif;font-weight:700;font-size:.8rem;
 transform:rotate(-3deg);letter-spacing:.03em;color:var(--verify)}
.stamp.bad{color:var(--ledger)}
/* assembly line */
.line-wrap{perspective:1400px;margin-bottom:1.1rem}
.line-scroll{overflow-x:auto;padding-bottom:.4rem}
.line-scroll svg{display:block;min-width:940px;width:100%;
 transform:rotateX(9deg);transform-origin:50% 0;font-family:'IBM Plex Sans',system-ui,sans-serif}
.station rect{fill:var(--sheet);stroke:var(--rule-strong);stroke-width:1;transition:stroke .2s}
.station.clickable{cursor:pointer}
.station.clickable:hover rect{stroke:var(--carbon)}
.station.live rect{animation:pulse 1.7s ease-in-out infinite;stroke:var(--signal)}
.station.failed rect{stroke:var(--ledger);stroke-width:1.6}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
.st-name{font-size:11.5px;font-weight:600;fill:var(--carbon)}
.st-count{font-family:'IBM Plex Serif',Georgia,serif;font-size:25px;font-weight:700;fill:var(--carbon)}
.st-sub{font-size:10.5px;fill:var(--muted)}
.st-sub.bad{fill:var(--ledger)}
.conn{stroke:var(--rule-strong);stroke-dasharray:3 4;stroke-width:1.2;fill:none}
.conn.ready{stroke:var(--signal);stroke-dasharray:none}
.empty-line{color:var(--muted);font-size:.9rem;margin:.3rem 0 0}
/* grid */
.grid{display:grid;grid-template-columns:19.5rem 1fr;gap:1.4rem;align-items:start}
@media (max-width:900px){.grid{grid-template-columns:1fr}}
/* runs rail */
.run-card{border:1px solid var(--rule);border-radius:6px;padding:.6rem .75rem;margin:.5rem 0;
 cursor:pointer;background:var(--sheet);width:100%;text-align:left}
.run-card:hover{border-color:var(--signal)}
.run-card.open{border-color:var(--signal);box-shadow:inset 0 0 0 1px var(--signal)}
.run-card .meta{display:block}
/* exhibits */
.exhibit{background:var(--sheet);border:1px solid var(--rule);border-left:3px solid var(--rule-strong);
 border-radius:4px;padding:.8rem .95rem;margin:.6rem 0}
.exhibit.v-ok{border-left-color:var(--verify)}
.exhibit.v-bad{border-left-color:var(--ledger)}
.exhibit.v-flag{border-left-color:var(--flag)}
.claim-text{font-family:'IBM Plex Serif',Georgia,serif;font-size:1.05rem;font-weight:600;margin:.4rem 0}
blockquote.excerpt{font-family:'IBM Plex Serif',Georgia,serif;font-style:italic;margin:.45rem 0;
 padding:.35rem .7rem;border-left:3px solid var(--rule-strong);color:var(--muted);font-size:.92rem}
ul.foot{list-style:none;margin:.5rem 0 0;padding:0}
ul.foot li{font-size:.84rem;margin:.18rem 0}
.fn{color:var(--signal);font-weight:600;margin-right:.25rem}
.issue{font-size:.85rem;margin:.35rem 0;display:flex;flex-wrap:wrap;gap:.35rem;align-items:center}
.actions{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.55rem}
.interp{border:1px dashed var(--rule-strong);background:var(--sheet);border-radius:4px;
 padding:.8rem .95rem;margin:.6rem 0}
.hyp-title{font-family:'IBM Plex Serif',Georgia,serif;font-weight:700;font-size:1.02rem;margin:.3rem 0}
dl.kv{margin:.5rem 0 0;font-size:.88rem}
dl.kv dt{font-weight:600;font-size:.8rem;color:var(--muted);margin-top:.45rem}
dl.kv dd{margin:0}
.err{color:var(--ledger)}
.srow{padding:.45rem 0;border-bottom:1px solid var(--rule);font-size:.88rem}
.srow:last-child{border-bottom:none}
#status{min-height:1.3rem;margin:.35rem 0 .8rem;font-size:.9rem;color:var(--verify);font-weight:500}
#status.err{color:var(--ledger)}
@media (prefers-reduced-motion: reduce){
 .line-scroll svg{transform:none}
 .station.live rect{animation:none}
 html{scroll-behavior:auto}
}
</style></head><body>
<main>
<header class="site">
 <span class="wordmark">PortfolioPulse</span>
 <span class="tagline">Evidence-first company research</span>
 <span style="margin-left:auto;display:flex;gap:.4rem;align-items:center">
  <span id="provider-chip" class="chip">providers…</span>
  <a href="/key" target="_blank">Provider key</a>
 </span>
</header>
<div id="status" role="status"></div>

<section aria-label="Research assembly line">
 <div class="line-wrap"><div class="line-scroll"><div id="line"></div></div></div>
 <p class="empty-line" id="line-note">Start a run and watch the line fill in — every count comes straight from the run's own data.</p>
</section>

<div class="grid">
 <div>
  <section class="panel" aria-label="Start a research run">
   <h2 style="margin-top:0">Start a run</h2>
   <label for="c-name">Company (pick an existing one or type a new name)</label>
   <input id="c-name" list="companies" placeholder="e.g. Northstar Components">
   <datalist id="companies"></datalist>
   <label for="c-website">Company website (optional, new companies)</label>
   <input id="c-website" placeholder="https://…">
   <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem">
    <span><label for="c-sector">Sector (optional)</label><input id="c-sector"></span>
    <span><label for="c-country">Country (optional)</label><input id="c-country"></span>
   </div>
   <label for="focus">Research focus</label>
   <textarea id="focus" placeholder="e.g. acquisition integration and geographic expansion"></textarea>
   <div class="actions"><button class="primary" onclick="startRun()">Start research</button></div>
   <p class="meta">No Gemini key? Everything still works offline via the demo fixtures.</p>
  </section>
  <section aria-label="Runs">
   <h2>Runs</h2>
   <div id="runs"></div>
  </section>
 </div>
 <section id="detail" aria-live="polite">
  <p class="meta" id="detail-empty">No run open. Start one, or open a run from the list.</p>
  <div id="run-panel"></div>
  <div id="claims-sec"></div>
  <div id="hyps-sec"></div>
  <div id="sources-sec"></div>
  <div id="exports-sec"></div>
 </section>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate=s=>s?String(s).slice(0,10):'?';
let currentRun=null,pollTimer=null;

async function jfetch(url,opts){const r=await fetch(url,opts);const b=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(b.detail||r.statusText);return b;}
function say(m,err=false){$('status').textContent=m;$('status').className=err?'err':'';}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}

const STATUS_WORD={queued:'Queued',running:'Researching',awaiting_review:'Needs review',
 approved:'Approved',failed:'Failed'};
const STATUS_CHIP={queued:'',running:'info',awaiting_review:'flag',approved:'ok',failed:'bad'};
const VER_CHIP={supported:'ok',conflicting:'bad',insufficient:'flag'};
const VER_SPINE={supported:'v-ok',conflicting:'v-bad',insufficient:'v-flag'};
const REV_STAMP={approved:'Approved',rejected:'Rejected',edited:'Corrected',flagged:'Flagged'};
const HYP_CHIP={draft:'flag',approved:'ok',rejected:'bad',flagged:'flag'};

async function loadProvider(){try{const d=await jfetch('/provider-status');
 const parts=[`${esc(d.search_provider)} search`,`${esc(d.llm_provider)} extraction`];
 let cls=d.llm_provider==='gemini'?'ok':'';
 $('provider-chip').textContent=parts.join(' · ');
 $('provider-chip').className='chip '+cls;}catch(e){$('provider-chip').textContent='provider status unavailable';}}

async function loadCompanies(){try{const d=await jfetch('/companies');
 $('companies').innerHTML=d.companies.map(c=>`<option value="${esc(c.name)}">`).join('');}catch(e){}}

const runCounts=r=>`sources ${r.source_count} · claims ${r.claim_count} · hypotheses ${r.hypothesis_count}`;
function statusChip(s){return `<span class="chip ${STATUS_CHIP[s]||''}">${esc(STATUS_WORD[s]||s)}</span>`;}

async function loadRuns(){try{const d=await jfetch('/runs');
 $('runs').innerHTML=d.runs.slice().reverse().map(r=>`
  <button type="button" class="run-card${r.run_id===currentRun?' open':''}" onclick="openRun('${esc(r.run_id)}')">
   ${statusChip(r.status)} <b>${esc((r.company||{}).name||'Run')}</b>
   <div class="meta">${esc(r.research_focus||'')}</div>
   <div class="meta">${esc(runCounts(r))}</div>
  </button>`).join('')||'<p class="meta">No runs yet.</p>';}catch(e){say('runs: '+e.message,true);}}

async function startRun(){
 say('starting…');
 const name=$('c-name').value.trim();
 if(!name)return say('company name is required',true);
 try{
  const list=await jfetch('/companies');
  let companyId=(list.companies.find(c=>c.name===name)||{}).company_id;
  if(!companyId){const c=await jfetch('/companies',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({name,website:$('c-website').value.trim()||null,
    sector:$('c-sector').value.trim()||null,country:$('c-country').value.trim()||null})});
   companyId=c.company_id;}
  const focus=$('focus').value.trim();
  if(focus.length<3)return say('research focus is required (min 3 chars)',true);
  const run=await jfetch('/research-runs',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({company_id:companyId,research_focus:focus})});
  say('run started — the line will fill in as it works');
  await loadRuns();loadCompanies();openRun(run.run_id);
 }catch(e){say('start failed: '+e.message,true);}}

function openRun(id){currentRun=id;stopPoll();loadRun();
 pollTimer=setInterval(()=>{if(!document.hidden)loadRun();},2500);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&currentRun)loadRun();});

/* ---- assembly line ---- */
const STATIONS=[
 {key:'scope',name:'Scope',sub:r=>r?(r.research_focus||'').slice(0,30):'no run open',count:r=>''},
 {key:'collect',name:'Collect',sub:()=>'sources',count:r=>r?r.source_count:''},
 {key:'extract',name:'Extract',sub:()=>'claims',count:r=>r?r.claim_count:''},
 {key:'validate',name:'Validate',sub:()=>'open issues',count:(r,u)=>r&&u?u.pending_issues:''},
 {key:'hypoth',name:'Hypothesize',sub:()=>'drafts',count:r=>r?r.hypothesis_count:''},
 {key:'review',name:'Review',sub:()=>'pending',count:(r,u)=>r&&u?u.pending_claims+u.open_hypotheses:''},
 {key:'approve',name:'Approve',sub:r=>r?STATUS_WORD[r.status].toLowerCase():'no run open',count:()=>''},
 {key:'export',name:'Export',sub:()=>'files',count:(r,x)=>r&&x&&x.exports?x.exports.files.length:(r&&r.status==='approved'?'ready':'')},
];
function renderLine(run,u,exportsInfo){
 const W=1166,H=132,SW=130,GAP=18,Y=22;
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Research pipeline status">
  <defs><marker id="arw" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
   <path d="M0,0 L8,4 L0,8 z" fill="#B9C3D1"/></marker></defs>`;
 let liveKey=null,failedKey=null;
 if(run){
  if(run.status==='queued'||run.status==='running')
   liveKey=run.source_count===0?'collect':run.claim_count===0?'extract':'review';
  if(run.status==='failed')
   failedKey=run.source_count===0?'collect':run.claim_count===0?'extract':'approve';
 }
 const ready=[null,run&&run.source_count>0,run&&run.claim_count>0,run&&u&&u.pending_issues!==undefined,
  run&&run.hypothesis_count>0,run&&u&&(u.pending_claims>0||u.open_hypotheses>0),run&&(run.status==='awaiting_review'||run.status==='approved'),
  run&&run.status==='approved'];
 STATIONS.forEach((st,i)=>{
  const x=i*(SW+GAP);
  const count=st.count(run,u,exportsInfo);
  const sub=st.sub(run);
  const subCls=(st.key==='approve'&&run&&run.status==='failed')?' st-sub bad':'';
  const cls='station'+(liveKey===st.key?' live':'')+(failedKey===st.key?' failed':'')+' clickable';
  let inner=`<text class="st-name" x="${x+SW/2}" y="${Y+22}" text-anchor="middle">${esc(st.name)}</text>`;
  if(st.key==='approve'&&run&&run.status==='approved')
   inner+=`<text class="st-count" x="${x+SW/2}" y="${Y+54}" text-anchor="middle" fill="#1F6F54" transform="rotate(-4 ${x+SW/2} ${Y+54})" font-size="17">APPROVED</text>`;
  else if(count!=='')
   inner+=`<text class="st-count" x="${x+SW/2}" y="${Y+54}" text-anchor="middle">${esc(count)}</text>`;
  inner+=`<text class="st-sub${subCls}" x="${x+SW/2}" y="${Y+76}" text-anchor="middle">${esc(sub||'—')}</text>`;
  svg+=`<g class="${cls}" data-key="${st.key}" transform="translate(0,0)"><rect x="${x}" y="${Y}" width="${SW}" height="84" rx="6"/>${inner}</g>`;
  if(i<STATIONS.length-1)
   svg+=`<path class="conn${ready[i+1]?' ready':''}" marker-end="url(#arw)" d="M${x+SW} ${Y+42} H${x+SW+GAP-8}"/>`;
 });
 svg+='</svg>';
 $('line').innerHTML=svg;
 $('line-note').textContent=run
  ?(run.error_summary?('Run hit a problem: '+run.error_summary):'Counts update automatically while a run works.')
  :'Start a run and watch the line fill in — every count comes straight from the run\\'s own data.';
 document.querySelectorAll('.station').forEach(g=>g.addEventListener('click',()=>{
  $('detail').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});}));
}

/* ---- detail ---- */
function unresolvedChips(u){
 const parts=[['claims pending review',u.pending_claims],['hypotheses open',u.open_hypotheses],['open issues',u.pending_issues]];
 const total=u.pending_claims+u.open_hypotheses+u.pending_issues;
 if(!total)return '<span class="chip ok">All clear — nothing unresolved</span>';
 return parts.filter(p=>p[1]>0).map(p=>`<span class="chip flag">${esc(p[0])}: ${p[1]}</span>`).join(' ');
}

function openEditor(kind,id,text){
 const el=$('ct-'+id);
 if(!el)return;
 el.innerHTML=`<textarea id="ed-${esc(id)}">${esc(text)}</textarea>
  <div class="actions"><button class="primary" onclick="saveEdit('${kind}','${esc(id)}')">Save correction</button>
  <button onclick="loadRun()">Cancel</button></div>`;
 $('ed-'+esc(id))&&$('ed-'+esc(id)).focus();
}
async function saveEdit(kind,id){
 const v=($('ed-'+id)?$('ed-'+id).value:'').trim();
 if(!v)return say('correction cannot be empty',true);
 await review(kind,id,'edited',v);
}

const REVIEW_BASE={claim:'claims',hypothesis:'hypotheses'};
async function review(kind,id,decision,editedText){
 try{
  await jfetch('/'+REVIEW_BASE[kind]+'/'+id+'/review',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({decision,edited_text:editedText||null})});
  say('review recorded: '+decision);loadRun();loadRuns();
 }catch(e){say('review failed: '+e.message,true);}}
async function reviewIssue(issueId,status){
 try{await jfetch('/validation-issues/'+issueId+'/status',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  say('issue '+status);loadRun();}catch(e){say('issue failed: '+e.message,true);}}
async function approveRun(id){
 try{const r=await jfetch('/research-runs/'+id+'/approve',{method:'POST'});
  say(r.message);loadRun();loadRuns();}catch(e){say('approve blocked: '+e.message,true);}}
async function exportRun(id){
 try{await jfetch('/research-runs/'+id+'/export',{method:'POST'});
  say('exports generated');loadRun();loadRuns();}catch(e){say('export failed: '+e.message,true);}}
async function resumeRun(id){
 try{await jfetch('/research-runs/'+id+'/resume',{method:'POST'});
  say('run resumed');loadRun();loadRuns();}catch(e){say('resume failed: '+e.message,true);}}

function footList(sources){return `<ul class="foot">`+(sources||[]).map((s,j)=>
 `<li><span class="fn">[${j+1}]</span>
  <a href="${esc(s.canonical_url)}" target="_blank" rel="noopener">${esc(s.publisher||s.title||s.canonical_url)}</a>
  <span class="meta">retrieved ${esc(fmtDate(s.retrieved_at))}, fetch ${esc(s.fetch_status)}</span></li>`).join('')+'</ul>';}

function issueRows(issues){return (issues||[]).map(i=>`<div class="issue">
 <span class="chip flag">${esc(i.issue_type)}</span>
 <span>${esc(i.message||'')}</span>
 ${i.review_status==='pending'
  ?`<button onclick="reviewIssue('${esc(i.issue_id)}','resolved')">mark resolved</button>
    <button onclick="reviewIssue('${esc(i.issue_id)}','waived')">waive</button>`
  :`<span class="meta">reviewer: ${esc(i.review_status)}</span>`}</div>`).join('');}

function claimCard(c){
 const spine=VER_SPINE[c.verification_status]||'';
 const decided=c.review_status!=='pending';
 return `<article class="exhibit ${spine}">
  <div class="actions" style="margin-top:0;gap:.35rem">
   <span class="chip">${esc(c.category)}</span>
   ${c.verification_status?`<span class="chip ${VER_CHIP[c.verification_status]||''}">${esc(c.verification_status)}</span>`:''}
   ${decided?`<span class="stamp${c.review_status==='rejected'?' bad':''}">${esc(REV_STAMP[c.review_status]||c.review_status)}</span>`:''}
  </div>
  <p class="claim-text" id="ct-${esc(c.claim_id)}">${esc(c.claim_text)}</p>
  ${c.supporting_excerpt?`<blockquote class="excerpt">${esc(c.supporting_excerpt)}</blockquote>`:''}
  ${footList(c.sources)}
  ${issueRows(c.validation_issues)}
  ${c.review_status==='pending'?`<div class="actions">
   <button class="primary" onclick="review('claim','${esc(c.claim_id)}','approved')">Approve claim</button>
   <button onclick="openEditor('claim','${esc(c.claim_id)}',$('raw-${esc(c.claim_id)}').value)">Edit wording</button>
   <button class="danger" onclick="review('claim','${esc(c.claim_id)}','rejected')">Reject</button>
   <button onclick="review('claim','${esc(c.claim_id)}','flagged')">Flag</button>
  </div>`:''}
  <input type="hidden" id="raw-${esc(c.claim_id)}" value="${esc(c.claim_text)}">
 </article>`;
}

function hypCard(h){
 const decided=h.status!=='draft';
 const list=(label,items)=>items&&items.length?`<dt>${esc(label)}</dt><dd>${esc(items.join(' — '))}</dd>`:'';
 return `<article class="interp">
  <div class="actions" style="margin-top:0;gap:.35rem">
   <span class="chip ${HYP_CHIP[h.status]||''}">${esc(h.status)}</span>
   ${decided?`<span class="stamp${h.status==='rejected'?' bad':''}">${esc(REV_STAMP[h.status]||h.status)}</span>`:''}
  </div>
  <p class="hyp-title">${esc(h.title)}</p>
  <p class="claim-text" id="ct-${esc(h.hypothesis_id)}" style="font-weight:400">${esc(h.hypothesis_text)}</p>
  <dl class="kv">${list('Evidence so far',h.evidence_summary)}
   ${list('Alternative explanations',h.alternative_explanations)}
   ${list('Missing evidence',h.missing_evidence)}
   ${list('Questions to validate',h.questions_to_validate)}</dl>
  ${h.status==='draft'?`<div class="actions">
   <button class="primary" onclick="review('hypothesis','${esc(h.hypothesis_id)}','approved')">Approve hypothesis</button>
   <button onclick="openEditor('hypothesis','${esc(h.hypothesis_id)}',$('raw-${esc(h.hypothesis_id)}').value)">Edit wording</button>
   <button class="danger" onclick="review('hypothesis','${esc(h.hypothesis_id)}','rejected')">Reject</button>
   <button onclick="review('hypothesis','${esc(h.hypothesis_id)}','flagged')">Flag</button>
  </div>`:''}
  <input type="hidden" id="raw-${esc(h.hypothesis_id)}" value="${esc(h.hypothesis_text)}">
 </article>`;
}

async function loadRun(){
 if(!currentRun)return;
 let run,rev,exportsInfo=null,sources=null;
 try{
  run=await jfetch('/research-runs/'+currentRun);
  rev=await jfetch('/runs/'+currentRun+'/review');
  if(run.status==='approved'){try{exportsInfo=await jfetch('/research-runs/'+currentRun+'/exports');}catch(e){}
   try{sources=await jfetch('/research-runs/'+currentRun+'/sources');}catch(e){}}
 }catch(e){say('detail: '+e.message,true);return;}
 const u=rev.unresolved||{};
 renderLine(run,u,exportsInfo);
 /* run panel */
 let html=`<div class="panel">
  <div class="actions" style="margin-top:0;gap:.35rem">${statusChip(run.status)}
   <b style="font-family:'IBM Plex Serif',Georgia,serif;font-size:1.05rem">${esc((run.company||{}).name||run.run_id)}</b></div>
  <p class="meta" style="margin:.35rem 0 0">Focus: ${esc(run.research_focus||'—')}</p>
  <p class="meta" style="margin:.15rem 0 0">${esc(runCounts(run))}${run.finished_at?` · finished ${esc(fmtDate(run.finished_at))}`:''}</p>
  ${run.delta?`<p class="meta" style="margin:.15rem 0 0"><b>Pulse vs run ${esc(run.delta.prior_run_id)}</b> · ${run.delta.unchanged_count} unchanged · ${run.delta.new.length} new · ${run.delta.contradicts.length} contradicting</p>`:''}
  ${run.status==='queued'||run.status==='running'?'<span class="meta">Measuring as the run works…</span>':unresolvedChips(u)}`;
 if(run.error_summary)html+=`<p class="err">${esc(run.error_summary)}</p>`;
 if(run.status==='queued'||run.status==='running')
  html+='<p class="meta">Working — this panel refreshes automatically.</p>';
 html+='<div class="actions">';
 if(run.status==='awaiting_review')
  html+=`<button class="primary" onclick="approveRun('${esc(run.run_id)}')">Approve for export</button>`;
 if(run.status==='awaiting_review'||run.status==='approved')
  html+=`<button onclick="exportRun('${esc(run.run_id)}')">Generate exports</button>`;
 if(run.status==='failed')
  html+=`<button onclick="resumeRun('${esc(run.run_id)}')">Resume run</button>`;
 html+='</div></div>';
 $('run-panel').innerHTML=html;
 /* sections */
 $('claims-sec').innerHTML=rev.claims.length
  ?`<h2>Claims</h2>${rev.claims.map(claimCard).join('')}`
  :(run.status==='queued'||run.status==='running'?'':'<h2>Claims</h2><p class="meta">No claims recorded for this run.</p>');
 $('hyps-sec').innerHTML=rev.hypotheses.length
  ?`<h2>Hypotheses</h2>${rev.hypotheses.map(hypCard).join('')}`
  :'';
 if(sources&&sources.sources){
  $('sources-sec').innerHTML=`<h2>Sources</h2><div class="panel">`+sources.sources.map(s=>
   `<div class="srow"><b>${esc(s.publisher||s.title||s.canonical_url)}</b>
    ${s.title?`<span class="meta">${esc(s.title)}</span>`:''}
    <div class="meta"><a href="${esc(s.canonical_url)}" target="_blank" rel="noopener">${esc(s.canonical_url)}</a>
    · ${esc(s.status)} · extraction ${esc(s.extraction_status||'?')} · retrieved ${esc(fmtDate(s.retrieved_at))}</div></div>`).join('')+'</div>';
 }
 if(exportsInfo&&exportsInfo.exports){
  $('exports-sec').innerHTML=`<h2>Exports</h2><div class="panel">
   ${exportsInfo.exports.files.map(f=>`<div class="srow">
    <a href="/research-runs/${esc(run.run_id)}/exports/${esc(f)}" target="_blank">${esc(f)}</a></div>`).join('')}
   <div class="srow"><a href="/research-runs/${esc(run.run_id)}/exports/manifest.json" target="_blank">manifest.json</a></div>
   <p class="meta">Each download is checked against the run's manifest.</p></div>`;
 }
 if(run.status!=='queued'&&run.status!=='running')stopPoll();
}

 renderLine(null,null,null);
 loadProvider();loadCompanies();loadRuns();
</script></body></html>"""
