import './styles.css';
import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

const state={
  sources:[],rows:[],reviewRows:[],stats:{total:0,mq4:0,mq5:0,review:0,duplicates:0,verified:0},
  scanning:false,view:'library',search:'',platform:'ALL',category:'ALL',
  page:0,reviewPage:0,pageSize:500,totalRows:0,totalReview:0,
  folderPreview:null,browserPath:null,browserData:null,sortBy:'name',sortDir:'asc'
};
let dbPath='';
const bootStarted=performance.now();
const categories=['Trend','Oscillator','Volume','Bill Williams','Volatility','Support/Resistance','Momentum','Signal','Price Action','Market Structure','Statistical','Utility','Custom / Specialized','Composite / Multi-Purpose','Unknown'];
const app=document.querySelector('#app');
const header=(label,key)=>`<th data-sort="${key}" style="cursor:pointer;user-select:none" title="Sort by ${label}">${label} <span class="sort-mark" data-sort-mark="${key}"></span></th>`;
app.innerHTML=`<div class="app"><aside class="sidebar"><div class="brand">MQL Indicator Library<small>0.4.0 • Evidence Engine v4</small></div><div class="nav"><button data-view="library" class="active">Library</button><button data-view="scan">Scan</button><button data-view="review">Review</button><button data-view="settings">Settings</button></div><div class="sidebar-footer" id="appStatus">Local library • Offline core</div></aside><main class="main"><div class="topbar"><input id="searchBox" class="search" placeholder="Search indicators, techniques, categories…"><button class="btn" id="refreshBtn">Refresh</button></div><div class="content">
<section id="library" class="view active"><div class="title-row"><div><h1>Indicator Library</h1><div class="muted">Truth-first classification with explicit evidence and abstention.</div></div></div><div class="cards"><div class="card"><div class="muted">All Indicators</div><div class="n" id="statTotal">0</div></div><div class="card"><div class="muted">MQL4</div><div class="n" id="statMq4">0</div></div><div class="card"><div class="muted">MQL5</div><div class="n" id="statMq5">0</div></div><div class="card"><div class="muted">Needs Review</div><div class="n" id="statReview">0</div></div><div class="card"><div class="muted">Verified</div><div class="n" id="statVerified">0</div></div></div><div class="toolbar"><select id="platformFilter"><option>ALL</option><option>MQL4</option><option>MQL5</option></select><select id="categoryFilter"><option>ALL</option>${categories.map(x=>`<option>${x}</option>`).join('')}</select><button class="btn" id="previewAllBtn">Prioritize filtered previews</button><div class="preview-cache-meter" title="Background preview cache progress"><div id="previewCacheBar"></div></div><span class="status" id="previewAllStatus"></span><span class="status" id="resultCount"></span></div><div class="table-wrap"><table><thead><tr>${header('Name','name')}${header('Platform','platform')}${header('Function','function')}${header('Status','status')}${header('Visual','visual')}${header('Confidence','confidence')}</tr></thead><tbody id="rows"></tbody></table><div class="empty" id="empty">No indicators yet. Add a folder from Scan.</div></div><div class="pager"><button class="btn" id="prevBtn">Previous</button><span id="pageInfo" class="status"></span><button class="btn" id="nextBtn">Next</button></div></section>
<section id="scan" class="view"><div class="title-row"><div><h1>Scan Library</h1><div class="muted">Browse folders inside the app. Files remain visible while you choose the folder to scan.</div></div></div><div class="scan-panel"><button class="btn" id="addFolderBtn">+ Add Folder</button><div class="sources" id="sources"></div><div id="folderPreview" class="folder-preview"><div class="muted">No folder preview yet.</div></div><div style="margin-top:14px"><button class="btn primary" id="scanBtn">Scan Library</button></div><div class="progress"><div id="progressBar"></div></div><div class="status" id="scanText">Ready</div><div class="scan-stats"><div class="scan-stat"><div class="muted">Processed</div><b id="processed">0</b></div><div class="scan-stat"><div class="muted">Skipped</div><b id="skipped">0</b></div><div class="scan-stat"><div class="muted">Failed</div><b id="failed">0</b></div><div class="scan-stat"><div class="muted">Current</div><b id="current">0 / 0</b></div></div></div></section>
<section id="review" class="view"><div class="title-row"><div><h1>Review Queue</h1><div class="muted">Only uncertain, unverified classifications appear here.</div></div></div><div class="review-note">Unknown is a valid truthful result. Verify or correct only when you know what the indicator does.</div><div class="table-wrap"><table><thead><tr>${header('Name','name')}${header('Platform','platform')}${header('Possible Function','function')}${header('Status','status')}${header('Confidence','confidence')}</tr></thead><tbody id="reviewRows"></tbody></table></div><div class="pager"><button class="btn" id="reviewPrevBtn">Previous</button><span id="reviewPageInfo" class="status"></span><button class="btn" id="reviewNextBtn">Next</button></div></section>
<section id="settings" class="view"><div class="title-row"><div><h1>Settings</h1><div class="muted">0.4.0 keeps the core offline and stores classification evidence locally.</div></div></div><div class="scan-panel"><h3>Performance</h3><p class="muted">Library browsing uses direct Rust → SQLite queries with 500-row pages, a 50-row virtual window, lazy thumbnails, and whole-library sorting.</p><div class="diagnostic-card" id="previewWorkerCard"><h3>Preview Pipeline</h3><p class="muted">One warm MT5 terminal runs the resident render EA. A separate headless MetaEditor pool compiles indicators in parallel. Changes apply on the next pipeline start.</p><div class="preview-settings-grid"><label style="grid-column:1/-1">MT5 data folder<input id="mt5DataDir" type="text" placeholder="%APPDATA%\\MetaQuotes\\Terminal\\<hash>" title="Logged-in MT5 data folder containing config/accounts.dat and chart history"></label><label>Render hang timeout (sec)<input id="previewItemTimeout" type="number" min="10" max="180" value="40"></label><label>Max render attempts<input id="previewMaxAttempts" type="number" min="1" max="10" value="2"></label><label>Compile workers<input id="previewWorkers" type="number" min="1" max="8" value="4" title="Parallel MetaEditor compile processes"></label><label>Render terminals<input id="previewRenderServers" type="number" value="1" disabled title="Resident-EA architecture uses exactly one warm terminal"></label></div><div style="display:flex;gap:8px;margin-top:12px"><button class="btn" id="savePreviewWorkerSettings">Save Preview Settings</button><button class="btn" id="pausePreviewWorker">Pause Worker</button></div><div class="status" id="previewWorkerSettingsStatus" style="margin-top:9px">RENDER_TERMINALS=1 • COMPILE_WORKERS=4</div></div><div class="diagnostic-card"><h3>Diagnostics</h3><p class="muted">Export a support bundle containing application logs, scan engine output, errors, database health, timings, delays, paths, counts and startup diagnostics. Indicator source code is not copied.</p><button class="btn primary" id="exportDiagBtn">Export Diagnostic Bundle</button><div class="status" id="diagStatus" style="margin-top:10px">Ready</div></div><h3>Data</h3><div class="muted">Database location</div><div id="dbLocation" style="margin-top:7px;word-break:break-all"></div></div></section></div></main><aside class="detail" id="detail"><button class="btn close" id="closeDetail">×</button><div id="detailBody"></div></aside></div>
<div id="folderBrowser" class="browser-overlay hidden"><div class="browser-modal"><div class="browser-header"><div><h2>Select Folder</h2><div class="muted">Folders and files are shown together. Double-click a folder to open it.</div></div><button class="btn" id="browserClose">×</button></div><div class="browser-toolbar"><button class="btn" id="browserPc">This PC</button><button class="btn" id="browserUp">↑ Up</button><div class="browser-path" id="browserPath">This PC</div></div><div class="browser-list" id="browserList"></div><div class="browser-footer"><span class="status" id="browserStatus"></span><div><button class="btn" id="browserCancel">Cancel</button><button class="btn primary" id="browserUse">Use This Folder</button></div></div></div></div>`;

const el=id=>document.getElementById(id);
const addFolderBtn=el('addFolderBtn'),scanBtn=el('scanBtn'),processed=el('processed'),skipped=el('skipped'),failed=el('failed'),current=el('current'),scanText=el('scanText'),progressBar=el('progressBar');

async function logEvent(level,event,details={},durationMs=null){if(!dbPath)return;try{await invoke('app_log',{dbPath,level,event,details,durationMs:durationMs==null?null:Math.round(durationMs)});}catch(e){console.error('diagnostic log failed',e);}}
async function timed(event,fn,details={}){const t=performance.now();try{const out=await fn();logEvent('INFO',event,{...details,ok:true},performance.now()-t);return out;}catch(e){logEvent('ERROR',event,{...details,ok:false,error:String(e)},performance.now()-t);throw e;}}
async function engine(args){const cmd=Command.sidecar('binaries/mql-engine',args);const out=await cmd.execute();if(out.code!==0)throw new Error(out.stderr||`Engine exited ${out.code}`);return out.stdout.trim();}

function clampInt(value,def,min,max){const n=Number.parseInt(value,10);return Number.isFinite(n)?Math.max(min,Math.min(max,n)):def;}
function defaultPreviewWorkers(){
  const cpuCap=Math.max(1,Math.min(8,Math.floor((navigator.hardwareConcurrency||8)/2)));
  return Math.min(4,cpuCap);
}
function previewWorkerConfig(){
  const timeout=clampInt(localStorage.getItem('preview.timeout')||el('previewItemTimeout')?.value,40,10,180);
  const attempts=clampInt(localStorage.getItem('preview.attempts')||el('previewMaxAttempts')?.value,2,1,10);
  const workers=clampInt(localStorage.getItem('preview.workers')||el('previewWorkers')?.value,defaultPreviewWorkers(),1,8);
  const mt5DataDir=(localStorage.getItem('preview.mt5DataDir')||el('mt5DataDir')?.value||'').trim();
  return {timeout,attempts,workers,renderServers:1,mt5DataDir};
}
function loadPreviewWorkerSettings(){
  const cfg=previewWorkerConfig();
  if(el('previewItemTimeout'))el('previewItemTimeout').value=String(cfg.timeout);
  if(el('previewMaxAttempts'))el('previewMaxAttempts').value=String(cfg.attempts);
  if(el('previewWorkers'))el('previewWorkers').value=String(cfg.workers);
  if(el('previewRenderServers'))el('previewRenderServers').value='1';
  if(el('mt5DataDir'))el('mt5DataDir').value=cfg.mt5DataDir;
}
function savePreviewWorkerSettings(){
  const cfg={
    timeout:clampInt(el('previewItemTimeout')?.value,40,10,180),
    attempts:clampInt(el('previewMaxAttempts')?.value,2,1,10),
    workers:clampInt(el('previewWorkers')?.value,defaultPreviewWorkers(),1,8),
    mt5DataDir:(el('mt5DataDir')?.value||'').trim()
  };
  localStorage.setItem('preview.timeout',String(cfg.timeout));
  localStorage.setItem('preview.attempts',String(cfg.attempts));
  localStorage.setItem('preview.workers',String(cfg.workers));
  localStorage.setItem('preview.mt5DataDir',cfg.mt5DataDir);
  loadPreviewWorkerSettings();
  el('previewWorkerSettingsStatus').textContent=`Saved • RENDER_TERMINALS=1 • HANG_TIMEOUT=${cfg.timeout}s • MAX_ATTEMPTS=${cfg.attempts} • COMPILE_WORKERS=${cfg.workers}`;
}
let previewWorkerPaused=false;
async function togglePreviewWorkerPause(){
  const button=el('pausePreviewWorker');button.disabled=true;
  try{
    const next=!previewWorkerPaused;
    const out=await invoke('preview_worker_pause',{dbPath,paused:next});
    previewWorkerPaused=!!out.paused;
    button.textContent=previewWorkerPaused?'Resume Worker':'Pause Worker';
    el('previewWorkerSettingsStatus').textContent=previewWorkerPaused?'Preview pipeline paused by user':`Preview pipeline resumed • RENDER_TERMINALS=1 • COMPILE_WORKERS=${previewWorkerConfig().workers}`;
    if(!previewWorkerPaused)void startPreviewLibraryWorker();
  }catch(e){
    el('previewWorkerSettingsStatus').textContent=`Pause/Resume failed: ${e}`;
  }finally{button.disabled=false;}
}

async function filteredIndicatorRows(){
  const rows=[];let offset=0;
  while(offset<state.totalRows){
    const args={...queryArgs(false,offset),limit:500};
    const out=await invoke('db_query',args);
    const chunk=out.rows||[];
    rows.push(...chunk);
    if(!chunk.length)break;
    offset+=chunk.length;
  }
  return rows;
}

async function prioritizePreviewPaths(paths,priority=100,force=false){
  const unique=[...new Set((paths||[]).filter(Boolean))];
  if(!unique.length||!dbPath)return;
  try{
    await invoke('preview_queue_update',{dbPath,paths:unique,priority,force});
  }catch(e){
    logEvent('ERROR','preview_priority_update_failed',{error:String(e),count:unique.length,priority,force});
  }
}

async function previewAllFiltered(){
  const button=el('previewAllBtn'),status=el('previewAllStatus');
  button.disabled=true;status.textContent='Prioritizing filtered preview cache…';
  try{
    const rows=await filteredIndicatorRows();
    if(!rows.length){status.textContent='No indicators in the current filter.';return;}
    await prioritizePreviewPaths(rows.map(r=>r.path),500,false);
    status.textContent=`Queued ${rows.length.toLocaleString()} filtered indicators for background preview`;
    void startPreviewLibraryWorker();
  }catch(e){
    status.textContent=`Preview queue failed: ${e.message||e}`;
    await logEvent('ERROR','preview_queue_failed',{error:String(e)});
  }finally{button.disabled=false;}
}

window.__mqlQueuePreview=prioritizePreviewPaths;
async function ensureDb(){await timed('ensure_database',()=>engine(['stats','--db',dbPath]));}
function queryArgs(reviewOnly,offset){return{dbPath,search:reviewOnly?'':state.search,platform:reviewOnly?'ALL':state.platform,category:reviewOnly?'ALL':state.category,reviewOnly,limit:state.pageSize,offset,sortBy:state.sortBy,sortDir:state.sortDir};}

function handleScanLine(line){try{const ev=JSON.parse(line);if(ev.type==='source_preflight'){scanText.textContent=`Preflight: ${ev.source_files||0} source files, ${ev.compiled_files||0} compiled files.`;}else if(ev.type==='scan_start'){current.textContent=`0 / ${ev.total||0}`;scanText.textContent=ev.total?`Found ${ev.total.toLocaleString()} classifiable source files.`:'No classifiable source files found.';}else if(['item','progress','error'].includes(ev.type)){current.textContent=`${ev.current||0} / ${ev.total||0}`;progressBar.style.width=`${ev.total?(ev.current/ev.total)*100:0}%`;if(ev.processed!==undefined)processed.textContent=ev.processed;if(ev.skipped!==undefined)skipped.textContent=ev.skipped;if(ev.failed!==undefined)failed.textContent=ev.failed;scanText.textContent=ev.filename||ev.error||'Scanning…';}else if(ev.type==='scan_complete'){processed.textContent=ev.processed;skipped.textContent=ev.skipped;failed.textContent=ev.failed;scanText.textContent=`Complete in ${ev.elapsed_seconds}s`;progressBar.style.width='100%';}else if(ev.type==='fatal'){scanText.textContent=`Scan failed: ${ev.error}`;logEvent('ERROR','scan_fatal',ev);}}catch(e){console.error('Invalid scanner output',line,e);logEvent('ERROR','scan_output_parse_failed',{line,error:String(e)});}}

function updatePreviewProgress(done,total){
  const d=Number(done||0),t=Number(total||0);
  const pct=t?Math.min(100,(d/t)*100):0;
  el('previewCacheBar').style.width=`${pct}%`;
  el('previewAllStatus').textContent=`Previews ${d.toLocaleString()} / ${t.toLocaleString()} ready`;
}
async function refreshPreviewQueueStats(){
  if(!dbPath)return;
  try{const s=await invoke('preview_queue_stats',{dbPath});updatePreviewProgress(s.ready,s.total);previewWorkerPaused=!!s.paused;if(el('pausePreviewWorker'))el('pausePreviewWorker').textContent=previewWorkerPaused?'Resume Worker':'Pause Worker';}catch{}
}
function handlePreviewLibraryLine(line){
  let ev;try{ev=JSON.parse(line);}catch{return;}
  if(ev.type==='fatal'&&ev.fatal_kind==='render_startup_config'){
    el('previewAllStatus').textContent=ev.error||'Render terminal configuration failed.';
    el('previewWorkerSettingsStatus').textContent=ev.error||'Set your MT5 data folder in Settings and verify Algo Trading is enabled.';
    logEvent('ERROR','preview_render_startup_config',ev);
  }else if(ev.type==='progress')updatePreviewProgress(ev.done,ev.total);
  else if(ev.type==='compile_progress'){
    el('previewWorkerSettingsStatus').textContent=`Compile pool: ${Number(ev.done||0).toLocaleString()} completed • ${Number(ev.remaining||0).toLocaleString()} remaining`;
  }else if(ev.type==='heartbeat'){
    if(ev.stage==='render_server'&&ev.inflight)el('previewWorkerSettingsStatus').textContent=`Warm render terminal active • ${ev.inflight}`;
  }else if(ev.ok&&ev.paused){
    const why=ev.reason==='low_disk'?'low disk space':ev.reason==='low_battery'?'low battery':'paused by user';
    el('previewWorkerSettingsStatus').textContent=`Preview worker paused: ${why}`;
    if(ev.reason==='paused_by_user'){previewWorkerPaused=true;el('pausePreviewWorker').textContent='Resume Worker';}
  }else if(ev.ok&&ev.finished)void refreshPreviewQueueStats();
}

async function startPreviewLibraryWorker(){
  if(!dbPath)return;
  await refreshPreviewQueueStats();
  const outDir=await join(await appDataDir(),'previews');
  const jobId=`library-${Date.now()}`;
  const cfg=previewWorkerConfig();
  try{
    const renderArgs=[
      'render-server','--db',dbPath,'--out',outDir,'--hang-timeout',String(cfg.timeout),'--attempts',String(cfg.attempts),'--job-id',jobId
    ];
    if(cfg.mt5DataDir)renderArgs.push('--mt5-data-dir',cfg.mt5DataDir);
    const out=await invoke('start_preview_library',{args:renderArgs,workers:cfg.workers});
    if(out?.already_running||out?.started)void refreshPreviewQueueStats();
  }catch(e){
    el('previewAllStatus').textContent=`Preview cache worker: ${e}`;
    logEvent('ERROR','preview_library_start_failed',{error:String(e)});
  }
}
window.__mqlStartPreviewLibraryWorker=startPreviewLibraryWorker;

async function setup(){loadPreviewWorkerSettings();const base=await appDataDir();dbPath=await join(base,'library.sqlite3');el('dbLocation').textContent=dbPath;await logEvent('INFO','app_start',{userAgent:navigator.userAgent});await listen('scan-engine-line',e=>handleScanLine(e.payload?.line??e.payload));await listen('scan-engine-stderr',e=>{const line=e.payload?.line??e.payload;console.error('scanner',line);logEvent('ERROR','scan_engine_stderr_ui',{line});if(state.scanning&&line)scanText.textContent=`Scanner: ${line}`;});await listen('scan-engine-done',async e=>{if(!state.scanning)return;const code=Number(e.payload?.code??-1);logEvent(code===0?'INFO':'ERROR','scan_engine_done_ui',{code});if(code!==0&&!scanText.textContent.startsWith('Scan failed:'))scanText.textContent=`Scan engine exited with code ${code}`;await finishScan();});await listen('preview-library-line',e=>handlePreviewLibraryLine(e.payload?.line??e.payload));await listen('preview-library-stderr',e=>{const line=e.payload?.line??e.payload;console.error('preview worker',line);logEvent('ERROR','preview_library_stderr_ui',{line,workerId:e.payload?.worker_id||null});});await listen('preview-library-restart',e=>{const p=e.payload||{};el('previewWorkerSettingsStatus').textContent=`${p.component||p.worker_id||'render-server'} restarted (${p.reason||'worker exit'}) • ${p.restart_count||0}/20`;logEvent('WARN','preview_library_restart_ui',p);});await listen('preview-library-restart-cap',e=>{const p=e.payload||{};el('previewWorkerSettingsStatus').textContent=`Preview pipeline restart cap reached for ${p.component||p.worker_id||'render-server'} — review diagnostics`;logEvent('ERROR','preview_library_restart_cap_ui',p);});await listen('preview-library-config-error',e=>{const p=e.payload||{};const msg=p.message||'Render terminal configuration failed.';el('previewAllStatus').textContent=msg;el('previewWorkerSettingsStatus').textContent=msg;logEvent('ERROR','preview_library_config_error_ui',p);});await listen('preview-library-done',async e=>{const code=Number(e.payload?.code??-1);logEvent(code===0?'INFO':'ERROR','preview_library_done_ui',{code});await refreshPreviewQueueStats();await reloadAll();});await ensureDb();await reloadAll();void startPreviewLibraryWorker();await logEvent('INFO','app_ready',{totalIndicators:state.stats.total||0},performance.now()-bootStarted);}
async function reloadAll(){const t=performance.now();await Promise.all([loadStats(),loadRows(),loadReview()]);logEvent('INFO','reload_all',{rows:state.rows.length,reviewRows:state.reviewRows.length},performance.now()-t);}
async function loadStats(){const t=performance.now();try{state.stats=await invoke('db_stats',{dbPath});if(!state.sources.length)state.sources=(state.stats.sources||[]).map(s=>s.path);renderStats();renderSources();logEvent('INFO','load_stats',{total:state.stats.total,backendElapsedMs:state.stats.elapsed_ms},performance.now()-t);}catch(e){console.error(e);el('appStatus').textContent=`Database error: ${e}`;logEvent('ERROR','load_stats_failed',{error:String(e)},performance.now()-t);}}
async function loadRows(){const offset=state.page*state.pageSize,t=performance.now();try{const out=await invoke('db_query',queryArgs(false,offset));state.rows=out.rows||[];state.totalRows=out.total||0;renderRows();void prioritizePreviewPaths(state.rows.map(r=>r.path),100,false);logEvent('INFO','load_library_page',{page:state.page,count:state.rows.length,total:state.totalRows,search:state.search,sortBy:state.sortBy,sortDir:state.sortDir},performance.now()-t);}catch(e){console.error(e);logEvent('ERROR','load_library_page_failed',{error:String(e)},performance.now()-t);}}
async function loadReview(){const offset=state.reviewPage*state.pageSize,t=performance.now();try{const out=await invoke('db_query',queryArgs(true,offset));state.reviewRows=out.rows||[];state.totalReview=out.total||0;renderReview();logEvent('INFO','load_review_page',{page:state.reviewPage,count:state.reviewRows.length,total:state.totalReview,sortBy:state.sortBy,sortDir:state.sortDir},performance.now()-t);}catch(e){console.error(e);logEvent('ERROR','load_review_page_failed',{error:String(e)},performance.now()-t);}}

function renderStats(){el('statTotal').textContent=state.stats.total||0;el('statMq4').textContent=state.stats.mq4||0;el('statMq5').textContent=state.stats.mq5||0;el('statReview').textContent=state.stats.review||0;el('statVerified').textContent=state.stats.verified||0;}
function renderSources(){const target=el('sources');target.innerHTML=state.sources.length?state.sources.map(s=>`<div class="source"><span>${esc(s)}</span><span class="muted">Selected</span></div>`).join(''):'<div class="muted">No folder added yet.</div>';}
function renderFolderPreview(){const box=el('folderPreview');const p=state.folderPreview;if(!p){box.innerHTML='<div class="muted">No folder preview yet.</div>';return;}const shown=p.preview_files||[];box.innerHTML=`<div class="preview-head"><b>${Number(p.total_files||0).toLocaleString()} files visible to the app</b><span class="muted">${p.access_errors?`${p.access_errors} access error(s)`:''}</span></div><div class="muted preview-path">${esc(p.path)}</div><div class="preview-list">${shown.length?shown.map(f=>`<div>${esc(f)}</div>`).join(''):'<div class="muted">No files found in this folder tree.</div>'}</div>${p.total_files>shown.length?`<div class="muted preview-more">Showing first ${shown.length.toLocaleString()} of ${Number(p.total_files).toLocaleString()} files.</div>`:''}`;}
function statusClass(s){return s==='Verified'||s==='High Confidence'?'high':s==='Probable'?'med':'low';}
function updateSortMarks(){document.querySelectorAll('[data-sort-mark]').forEach(x=>{x.textContent=x.dataset.sortMark===state.sortBy?(state.sortDir==='asc'?'▲':'▼'):'';});}
const VIRTUAL_ROW_HEIGHT=64;
const VIRTUAL_WINDOW=50;
let previewThumbObserver=null;

function thumbPath(full){
  const s=String(full||'');const i=s.lastIndexOf('.');
  return i>0?s.slice(0,i)+'.thumb.png':s+'.thumb.png';
}
function previewStateText(r){
  if(r.preview_status==='ready')return 'Preview ready';
  if(r.preview_status==='compiling')return 'Compiling preview…';
  if(r.preview_status==='compiled')return 'Compiled • waiting for render';
  if(r.preview_status==='rendering')return 'Rendering in warm terminal…';
  if(r.preview_status==='failed'&&String(r.preview_error||'').includes('indicator OnInit failed (err 4802)'))return "Can't preview (indicator won't initialize)";
  if(r.preview_status==='failed')return "Can't preview";
  return 'Preview pending';
}
function observePreviewThumbs(target){
  const wrap=target.closest('.table-wrap');
  if(!('IntersectionObserver' in window)){
    target.querySelectorAll('img[data-preview-src]').forEach(img=>{img.src=convertFileSrc(img.dataset.previewSrc);});
    return;
  }
  if(previewThumbObserver)previewThumbObserver.disconnect();
  previewThumbObserver=new IntersectionObserver(entries=>{
    for(const entry of entries){
      if(!entry.isIntersecting)continue;
      const img=entry.target;const src=img.dataset.previewSrc;const full=img.dataset.previewFull;
      if(src){
        img.onerror=()=>{if(full&&img.dataset.fallback!=='1'){img.dataset.fallback='1';img.src=convertFileSrc(full);}else{img.style.display='none';}};
        img.src=convertFileSrc(src);
      }
      previewThumbObserver.unobserve(img);
    }
  },{root:wrap,rootMargin:'160px'});
  target.querySelectorAll('img[data-preview-src]').forEach(img=>previewThumbObserver.observe(img));
}
function renderRows(){renderTable(state.rows,el('rows'),false);el('resultCount').textContent=`${state.totalRows.toLocaleString()} matched`;el('empty').style.display=state.totalRows?'none':'block';const pages=Math.max(1,Math.ceil(state.totalRows/state.pageSize));el('pageInfo').textContent=`Page ${state.page+1} of ${pages}`;el('prevBtn').disabled=state.page===0;el('nextBtn').disabled=(state.page+1)*state.pageSize>=state.totalRows;updateSortMarks();}
function renderReview(){renderTable(state.reviewRows,el('reviewRows'),true);const pages=Math.max(1,Math.ceil(state.totalReview/state.pageSize));el('reviewPageInfo').textContent=`Page ${state.reviewPage+1} of ${pages} • ${state.totalReview.toLocaleString()} items`;el('reviewPrevBtn').disabled=state.reviewPage===0;el('reviewNextBtn').disabled=(state.reviewPage+1)*state.pageSize>=state.totalReview;updateSortMarks();}
function renderTable(rows,target,review){
  const wrap=target.closest('.table-wrap');if(!wrap)return;
  const start=Math.max(0,Math.floor(wrap.scrollTop/VIRTUAL_ROW_HEIGHT)-8);
  const end=Math.min(rows.length,start+VIRTUAL_WINDOW);
  const top=start*VIRTUAL_ROW_HEIGHT,bottom=Math.max(0,(rows.length-end)*VIRTUAL_ROW_HEIGHT);
  const cols=review?5:6;
  const body=rows.slice(start,end).map((r,n)=>{
    const i=start+n;
    const ready=r.preview_status==='ready'&&r.preview_path;
    const preview=ready?`<img class="preview-thumb" data-preview-src="${esc(thumbPath(r.preview_path))}" data-preview-full="${esc(r.preview_path)}" alt="" loading="lazy"/>`:`<span class="preview-placeholder ${r.preview_status==='failed'?'failed':''}"></span>`;
    return `<tr data-idx="${i}"><td><div class="indicator-name-cell">${preview}<div><div>${esc(r.filename)}</div><div class="preview-row-state">${esc(previewStateText(r))}</div></div></div></td><td><span class="badge">${esc(r.platform)}</span></td><td>${esc(r.primary_category)}</td><td><span class="confidence ${statusClass(r.classification_status)}">${esc(r.classification_status)}</span></td>${review?'':`<td>${esc(r.visual_category)}</td>`}<td class="confidence ${r.confidence>=85?'high':r.confidence>=70?'med':'low'}">${r.confidence}%</td></tr>`;
  }).join('');
  target.innerHTML=`<tr class="virtual-spacer"><td colspan="${cols}" style="height:${top}px"></td></tr>${body}<tr class="virtual-spacer"><td colspan="${cols}" style="height:${bottom}px"></td></tr>`;
  target.querySelectorAll('tr[data-idx]').forEach(tr=>tr.onclick=()=>showDetail(rows[Number(tr.dataset.idx)]));
  observePreviewThumbs(target);
  wrap.__virtualRender=()=>renderTable(rows,target,review);
  if(!wrap.dataset.virtualized){
    wrap.dataset.virtualized='1';let scheduled=false;
    wrap.addEventListener('scroll',()=>{if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;wrap.__virtualRender?.();});},{passive:true});
  }
}

function structuralPreview(r){
  const lines=Math.min(3,Number(r.line_plots||0)), hist=Number(r.histogram_plots||0)>0, arrows=Number(r.arrow_plots||0)>0, fill=Number(r.filling_plots||0)>0, objects=!!r.object_usage;
  const grid=`<g opacity=".18"><path d="M20 38H300M20 76H300M20 114H300M70 18V132M130 18V132M190 18V132M250 18V132" stroke="currentColor" stroke-width="1"/></g>`;
  let shapes='';
  if(fill)shapes+=`<path d="M20 70 C60 30 95 80 135 48 S215 35 300 58 L300 96 C245 75 205 112 145 86 S65 105 20 92 Z" fill="currentColor" opacity=".10"/>`;
  const paths=[`M20 92 C55 58 82 105 118 70 S178 45 210 76 S260 48 300 64`,`M20 106 C58 82 92 95 126 88 S180 70 220 94 S270 74 300 84`,`M20 78 C58 95 90 62 126 82 S184 108 224 72 S270 90 300 70`];
  for(let i=0;i<lines;i++)shapes+=`<path d="${paths[i]}" fill="none" stroke="currentColor" stroke-width="${i===0?2.5:1.5}" opacity="${1-i*.22}"/>`;
  if(hist){for(let i=0;i<14;i++){const h=12+((i*17)%42);shapes+=`<rect x="${26+i*19}" y="${118-h}" width="10" height="${h}" fill="currentColor" opacity=".42"/>`;}}
  if(arrows){[62,146,236].forEach((x,i)=>{const y=[55,96,48][i];shapes+=`<path d="M${x} ${y} l-6 10 h4 v10 h4 v-10 h4 z" fill="currentColor" opacity=".85"/>`;});}
  if(objects)shapes+=`<path d="M28 46H292M40 102L278 40" stroke="currentColor" stroke-width="1.5" stroke-dasharray="6 4" opacity=".6"/><rect x="190" y="66" width="75" height="34" fill="none" stroke="currentColor" opacity=".55"/>`;
  if(!shapes)shapes=`<text x="160" y="78" text-anchor="middle" fill="currentColor" opacity=".6" font-size="13">No static plot shape detected</text>`;
  return `<div class="section"><div class="label">Structural Preview</div><div style="border:1px solid rgba(148,163,184,.22);border-radius:10px;padding:10px;background:rgba(15,23,42,.35)"><svg viewBox="0 0 320 150" width="100%" height="170" role="img" aria-label="Indicator structural preview">${grid}${shapes}</svg><div class="muted" style="font-size:11px;margin-top:4px">Derived from source plot metadata (${esc(r.visual_category)} • ${esc(r.display_location)}). This is not live market output.</div></div></div>`;
}
function evidenceHtml(items){if(!items?.length)return '<div class="muted">No reliable functional evidence recorded.</div>';return items.slice(0,30).map(e=>`<div class="evidence"><b>${esc(e.category)}</b> <span class="muted">+${esc(e.weight)}</span><br>${esc(e.detail)}</div>`).join('');}
function showDetail(r){const secondary=r.secondary_categories||[];const tags=[...(r.behavior_tags||[]),...(r.techniques||[])];const options=categories.map(c=>`<option ${c===r.primary_category?'selected':''}>${c}</option>`).join('');const detail=el('detail');detail.dataset.previewStatus=r.preview_status||'';detail.dataset.previewPath=r.preview_path||'';detail.dataset.previewHash=r.preview_hash||'';detail.dataset.previewError=r.preview_error||'';detail.dataset.previewAttempts=String(r.preview_attempts||0);detail.dataset.sourceHash=r.sha256||'';void prioritizePreviewPaths([r.path],1000,false);void startPreviewLibraryWorker();el('detailBody').innerHTML=`<h2>${esc(r.filename)}</h2>${structuralPreview(r)}<div class="section"><div class="label">Classification</div><div class="value">${esc(r.primary_category)} • ${r.confidence}%</div><div class="value confidence ${statusClass(r.classification_status)}">${esc(r.classification_status)}</div>${r.review_reason?`<div class="muted detail-note">${esc(r.review_reason)}</div>`:''}</div><div class="section"><div class="label">Secondary Functions</div><div class="value">${secondary.map(x=>`<span class="badge">${esc(x)}</span>`).join(' ')||'None'}</div></div><div class="section"><div class="label">Evidence</div>${evidenceHtml(r.evidence)}</div><div class="section"><div class="label">Techniques & Behavior</div><div class="value">${tags.map(x=>`<span class="badge">${esc(x)}</span>`).join(' ')||'None detected'}</div></div><div class="section"><div class="label">Visual / Source</div><div class="value">${esc(r.visual_category)} • ${esc(r.display_location)}<br>${esc(r.platform)} • ${esc(r.source_structure)}<br>Buffers: ${r.active_buffers||0}/${r.declared_buffers||0} active/declared • Plots: ${r.declared_plots||0}</div></div><div class="section"><div class="label">Verification</div><select id="verifyPrimary" class="detail-select">${options}</select><button id="verifyBtn" class="btn primary verify-btn">${r.human_verified?'Update Verification':'Verify / Correct'}</button><div class="muted detail-note">Verification is stored separately from machine classification and becomes classification-memory input.</div></div><div class="section"><div class="label">File</div><div class="value" style="word-break:break-all">${esc(r.path)}</div></div>${(r.warnings||[]).length?`<div class="section"><div class="label">Warnings</div><div class="value">${r.warnings.map(esc).join('<br>')}</div></div>`:''}`;el('detail').classList.add('open');el('verifyBtn').onclick=()=>verifyIndicator(r);}
async function verifyIndicator(r){const primary=el('verifyPrimary').value,t=performance.now();try{await invoke('db_verify',{dbPath,indicatorId:r.id,primaryCategory:primary,secondaryCategories:r.secondary_categories||[]});logEvent('INFO','verify_indicator',{id:r.id,primary},performance.now()-t);el('detail').classList.remove('open');await reloadAll();}catch(e){logEvent('ERROR','verify_indicator_failed',{id:r.id,error:String(e)},performance.now()-t);alert(`Verification failed: ${e}`);}}

function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function switchView(name){state.view=name;document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===name));document.querySelectorAll('.nav button').forEach(x=>x.classList.toggle('active',x.dataset.view===name));logEvent('INFO','view_change',{view:name});}
async function browseTo(path=null){el('browserStatus').textContent='Loading…';const t=performance.now();try{const data=await invoke('browse_directory',{path});state.browserData=data;state.browserPath=data.path==='This PC'?null:data.path;renderBrowser();logEvent('INFO','browse_directory',{path:data.path,items:(data.entries||[]).length,backendElapsedMs:data.elapsed_ms},performance.now()-t);}catch(e){el('browserStatus').textContent=`Cannot open folder: ${e}`;logEvent('ERROR','browse_directory_failed',{path,error:String(e)},performance.now()-t);}}
function renderBrowser(){const data=state.browserData;if(!data)return;el('browserPath').textContent=data.path||'This PC';const entries=data.entries||[];el('browserStatus').textContent=`${entries.length.toLocaleString()} item${entries.length===1?'':'s'}`;el('browserUse').disabled=!state.browserPath;el('browserList').innerHTML=entries.length?entries.map((x,i)=>`<div class="browser-row ${x.kind==='folder'||x.kind==='drive'?'is-folder':'is-file'}" data-i="${i}"><span class="browser-icon">${x.kind==='drive'?'💽':x.kind==='folder'?'📁':'📄'}</span><span class="browser-name">${esc(x.name)}</span><span class="browser-kind">${x.kind==='file'?formatBytes(x.size||0):x.kind}</span></div>`).join(''):'<div class="browser-empty">This folder contains no items.</div>';el('browserList').querySelectorAll('.browser-row').forEach(row=>{const item=entries[Number(row.dataset.i)];if(item.kind==='folder'||item.kind==='drive')row.ondblclick=()=>browseTo(item.path);});}
function formatBytes(n){if(!n)return '0 B';const units=['B','KB','MB','GB'];let i=0,v=n;while(v>=1024&&i<units.length-1){v/=1024;i++;}return `${v.toFixed(i?1:0)} ${units[i]}`;}
async function openFolderBrowser(){el('folderBrowser').classList.remove('hidden');await browseTo(null);}
function closeFolderBrowser(){el('folderBrowser').classList.add('hidden');}
async function useBrowserFolder(){if(!state.browserPath)return;const p=state.browserPath;state.sources=[p];renderSources();closeFolderBrowser();scanText.textContent='Reading folder contents…';const t=performance.now();try{state.folderPreview=await invoke('folder_preview',{path:p,maxPreview:250});renderFolderPreview();scanText.textContent=`Folder ready: ${Number(state.folderPreview.total_files||0).toLocaleString()} files visible.`;logEvent('INFO','folder_preview',{path:p,totalFiles:state.folderPreview.total_files,accessErrors:state.folderPreview.access_errors,backendElapsedMs:state.folderPreview.elapsed_ms},performance.now()-t);}catch(e){state.folderPreview={path:p,total_files:0,preview_files:[],access_errors:1};renderFolderPreview();scanText.textContent=`Folder preview failed: ${e}`;logEvent('ERROR','folder_preview_failed',{path:p,error:String(e)},performance.now()-t);}}
async function startScan(){if(!state.sources.length){scanText.textContent='Add a folder first.';return;}if(state.scanning)return;state.scanning=true;scanBtn.disabled=true;addFolderBtn.disabled=true;processed.textContent=skipped.textContent=failed.textContent='0';current.textContent='0 / 0';scanText.textContent='Starting scan engine…';progressBar.style.width='0%';const args=['scan','--db',dbPath,...state.sources.flatMap(s=>['--source',s])];logEvent('INFO','scan_requested',{sources:state.sources});try{await invoke('start_scan_engine',{args});}catch(e){scanText.textContent=`Scan failed to start: ${e}`;logEvent('ERROR','scan_start_failed',{error:String(e),sources:state.sources});await finishScan();}}
async function finishScan(){state.scanning=false;scanBtn.disabled=false;addFolderBtn.disabled=false;state.page=0;state.reviewPage=0;await reloadAll();void startPreviewLibraryWorker();}
async function exportDiagnostics(){const btn=el('exportDiagBtn'),status=el('diagStatus');btn.disabled=true;status.textContent='Building diagnostic ZIP…';const t=performance.now();try{await logEvent('INFO','diagnostic_export_requested',{});const out=await invoke('export_diagnostics',{dbPath});status.textContent=`Exported: ${out.path}`;await logEvent('INFO','diagnostic_export_ui_complete',{path:out.path},performance.now()-t);}catch(e){status.textContent=`Diagnostic export failed: ${e}`;await logEvent('ERROR','diagnostic_export_ui_failed',{error:String(e)},performance.now()-t);}finally{btn.disabled=false;}}

function changeSort(key){if(state.sortBy===key)state.sortDir=state.sortDir==='asc'?'desc':'asc';else{state.sortBy=key;state.sortDir=key==='confidence'?'desc':'asc';}state.page=0;state.reviewPage=0;loadRows();loadReview();logEvent('INFO','sort_change',{sortBy:state.sortBy,sortDir:state.sortDir});}
window.addEventListener('error',e=>logEvent('ERROR','window_error',{message:e.message,filename:e.filename,line:e.lineno,column:e.colno}));
window.addEventListener('unhandledrejection',e=>logEvent('ERROR','unhandled_rejection',{reason:String(e.reason)}));
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>switchView(b.dataset.view));
document.querySelectorAll('th[data-sort]').forEach(h=>h.onclick=()=>changeSort(h.dataset.sort));
el('searchBox').oninput=()=>{clearTimeout(window.__st);window.__st=setTimeout(()=>{state.search=el('searchBox').value;state.page=0;loadRows();},180);};
el('platformFilter').onchange=()=>{state.platform=el('platformFilter').value;state.page=0;loadRows();};el('categoryFilter').onchange=()=>{state.category=el('categoryFilter').value;state.page=0;loadRows();};
el('refreshBtn').onclick=reloadAll;el('previewAllBtn').onclick=previewAllFiltered;el('savePreviewWorkerSettings').onclick=savePreviewWorkerSettings;el('pausePreviewWorker').onclick=togglePreviewWorkerPause;addFolderBtn.onclick=openFolderBrowser;scanBtn.onclick=startScan;el('exportDiagBtn').onclick=exportDiagnostics;el('closeDetail').onclick=()=>el('detail').classList.remove('open');
el('prevBtn').onclick=()=>{if(state.page>0){state.page--;loadRows();}};el('nextBtn').onclick=()=>{if((state.page+1)*state.pageSize<state.totalRows){state.page++;loadRows();}};
el('reviewPrevBtn').onclick=()=>{if(state.reviewPage>0){state.reviewPage--;loadReview();}};el('reviewNextBtn').onclick=()=>{if((state.reviewPage+1)*state.pageSize<state.totalReview){state.reviewPage++;loadReview();}};
el('browserClose').onclick=closeFolderBrowser;el('browserCancel').onclick=closeFolderBrowser;el('browserPc').onclick=()=>browseTo(null);el('browserUp').onclick=()=>{if(state.browserData?.parent)browseTo(state.browserData.parent);else browseTo(null);};el('browserUse').onclick=useBrowserFolder;
setup();
