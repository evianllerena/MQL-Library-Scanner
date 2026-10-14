import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';


let previewToken=0;
let previewTimer=null;
let activeJob=null;
let cancellationBarrier=Promise.resolve();
let desiredSource=null;
let previewState={source:null,phase:'idle',message:'',image:null,kind:null};
let clearConfirmUntil=0;
let lastStallLog=0;

async function dbPath(){return join(await appDataDir(),'library.sqlite3');}
async function appLog(level,event,details={}){
  try{await invoke('app_log',{dbPath:await dbPath(),level,event,details,durationMs:null});}catch{}
}

function parsePayload(text){
  const lines=String(text||'').trim().split(/\r?\n/).filter(Boolean);
  for(let i=lines.length-1;i>=0;i--){try{return JSON.parse(lines[i]);}catch{}}
  return null;
}

async function execSidecar(name,args){
  const out=await Command.sidecar(name,args).execute();
  const payload=parsePayload(out.stdout);
  if(out.code!==0||!payload?.ok){
    const error=new Error(payload?.error||payload?.output||out.stderr||`${name} exited ${out.code}`);
    error.payload=payload;
    throw error;
  }
  return payload;
}

function detailSource(){
  const detail=document.getElementById('detail');
  if(!detail)return null;
  for(const section of detail.querySelectorAll('.section')){
    if(section.querySelector('.label')?.textContent?.trim()==='File')return section.querySelector('.value')?.textContent?.trim()||null;
  }
  return null;
}

function newJobId(){
  try{return crypto.randomUUID().replace(/[^A-Za-z0-9_.-]/g,'-');}
  catch{return `job-${Date.now()}-${Math.random().toString(16).slice(2)}`;}
}

function currentPreviewCard(source){
  const card=document.getElementById('realMetaPreview');
  return card?.dataset.source===source?card:null;
}

function applyPreviewState(){
  const s=previewState;
  if(!s.source)return;
  const card=currentPreviewCard(s.source);
  if(!card)return;
  const status=card.querySelector('#previewStatus');
  const img=card.querySelector('#previewImage');
  const retry=card.querySelector('#retryPreview');
  if(status)status.textContent=s.message||'Ready';
  if(img){
    if(s.image){
      img.src=s.image;
      img.style.display='block';
    }else{
      img.style.display='none';
      img.removeAttribute('src');
    }
  }
  if(retry)retry.style.display=s.phase==='failed'?'inline-flex':'none';
}

function setPreviewState(source,patch){
  if(previewState.source!==source)previewState={source,phase:'idle',message:'',image:null,kind:null};
  previewState={...previewState,...patch,source};
  applyPreviewState();
}

async function cancelActive(reason){
  const job=activeJob;
  if(!job)return {ok:true,cancelled:false,reason:'no active preview'};

  await appLog('INFO','preview_cancel_requested',{
    jobId:job.jobId,pid:job.pid,source:job.source,reason
  });

  let result;
  try{
    result=await execSidecar('binaries/mql-preview-control',['cancel','--pid',String(job.pid)]);
  }catch(e){
    await appLog('ERROR','preview_cancel_failed',{
      jobId:job.jobId,pid:job.pid,source:job.source,reason,error:String(e)
    });
    throw new Error(`Previous preview process could not be safely stopped: ${e.message||e}`);
  }

  if(!result.cancelled||!result.verified_gone){
    await appLog('ERROR','preview_cancel_unverified',{
      jobId:job.jobId,pid:job.pid,source:job.source,reason,result
    });
    throw new Error('Previous preview process did not terminate cleanly. No new preview was started.');
  }

  if(activeJob?.jobId===job.jobId)activeJob=null;
  await appLog('INFO','preview_cancel_result',{
    jobId:job.jobId,pid:job.pid,source:job.source,reason,
    cancelled:true,verifiedGone:true,alreadyExited:!!result.already_exited,
    returncode:result.returncode??null,elapsedMs:result.elapsed_ms??null
  });
  return result;
}

function queueCancellation(reason){
  cancellationBarrier=cancellationBarrier
    .catch(()=>{})
    .then(()=>cancelActive(reason));
  return cancellationBarrier;
}

function invalidatePreview(reason){
  desiredSource=null;
  previewToken++;
  if(previewTimer){clearTimeout(previewTimer);previewTimer=null;}
  return queueCancellation(reason);
}

window.__mqlPreviewCancelForBatch=()=>invalidatePreview('batch preview starting');

// P2: cancellation rejects by design while a render process is still terminating -- handle it at the call
// sites instead of leaking an unhandled rejection, and tell the user what is going on
function cancelPreviewQuietly(reason){
  invalidatePreview(reason).catch(e=>{
    void appLog('WARN','preview_cancel_pending',{reason,error:String(e?.message||e)});
    const s=document.getElementById('previewStatus');
    if(s)s.textContent='Render process still terminating…';
  });
}
window.addEventListener('mql-batch-preview-result',event=>{
  const result=event.detail||{};
  if(!result.source||detailSource()!==result.source||!result.image)return;
  desiredSource=null;
  previewToken++;
  if(previewTimer){clearTimeout(previewTimer);previewTimer=null;}
  setPreviewState(result.source,{
    phase:'success',
    kind:result.kind||'MT5',
    image:convertFileSrc(result.image)+`?t=${Date.now()}`,
    message:`Real ${result.kind||'MT5'} preview • batch render`
  });
});

function previewStageMessage(stage,payload){
  if(payload?.message)return payload.message;
  const labels={
    terminal_selected:'Matched the active MetaTrader terminal and data folder.',
    runtime_preflight_cached:'Using validated preview runtime selection.',
    runtime_clone:'Preparing isolated MetaTrader runtime from the matched terminal…',
    mt5_prime_start:'Initializing isolated MT5 runtime…',
    mt5_full_recompile:'MetaTrader is compiling its MQL5 runtime for first use…',
    mt5_prime_ready:'MT5 runtime initialization completed.',
    mt5_prime_cached:'MT5 runtime is ready.',
    indicator_compile:'Compiling/staging selected indicator…',
    indicator_staged:'Indicator copied into the isolated preview runtime.',
    indicator_compile_success:'Indicator compile/stage succeeded.',
    capture_compile:'Compiling screenshot capture script…',
    terminal_launch:'Launching isolated MetaTrader terminal…',
    screenshot_wait:'Waiting for MetaTrader screenshot…',
    screenshot_ready:'Screenshot captured.'
  };
  return labels[stage]||`Preview engine: ${stage}`;
}

async function openSource(source,status,button){
  button.disabled=true;
  try{
    const out=await execSidecar('binaries/mql-preview',['open-source','--source',source]);
    status.textContent=`Opened in ${out.kind}: ${out.opened}`;
  }catch(e){
    status.textContent=`Could not open MetaTrader/MetaEditor: ${e.message}`;
  }finally{button.disabled=false;}
}

function detailCachedPreview(detail){
  const status=detail?.dataset?.previewStatus||'';
  const path=detail?.dataset?.previewPath||'';
  return status==='ready'&&path?{status,path}:null;
}

function injectPreview(){
  const detail=document.getElementById('detail');
  const body=document.getElementById('detailBody');
  if(!detail?.classList.contains('open')||!body)return;
  const source=detailSource();
  if(!source)return;
  const cached=detailCachedPreview(detail);

  const existing=document.getElementById('realMetaPreview');
  if(existing?.dataset.source===source){
    applyPreviewState();
    return;
  }

  existing?.remove();
  for(const old of [...body.querySelectorAll('#v5RealPreview,#v57RealPreview')])old.remove();

  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const section=document.createElement('div');
  section.className='section';
  section.id='realMetaPreview';
  section.dataset.source=source;
  section.innerHTML=`<div class="label">Real MetaTrader Preview</div><div style="border:1px solid rgba(127,127,127,.22);border-radius:10px;padding:10px;margin-top:8px"><div class="muted" style="margin-bottom:9px">Cached previews load instantly. MetaTrader only starts here when no ready cache exists or you explicitly refresh.</div><div class="status" id="previewStatus">Checking preview cache…</div><img id="previewImage" alt="MetaTrader indicator preview" style="display:none;width:100%;margin-top:10px;border-radius:8px;border:1px solid rgba(127,127,127,.25)"/><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="btn" id="retryPreview" style="display:none">Retry Preview</button><button class="btn" id="refreshPreview">Refresh Preview</button><button class="btn" id="openPreviewSource">Open in MetaTrader / MetaEditor</button></div></div>`;
  body.prepend(section);

  const status=section.querySelector('#previewStatus');
  const retry=section.querySelector('#retryPreview');
  const refresh=section.querySelector('#refreshPreview');
  const open=section.querySelector('#openPreviewSource');
  const queueRefresh=async(button)=>{
    button.disabled=true;
    try{
      if(typeof window.__mqlQueuePreview==='function')await window.__mqlQueuePreview([source],1000,true);
      if(typeof window.__mqlStartPreviewLibraryWorker==='function')void window.__mqlStartPreviewLibraryWorker();
      const cachedPath=detail.dataset.previewPath||'';
      detail.dataset.previewStatus='pending';
      detail.dataset.previewError='';
      desiredSource=null;
      previewToken++;
      if(previewTimer){clearTimeout(previewTimer);previewTimer=null;}
      previewState={
        source,
        phase:cachedPath?'success':'pending',
        kind,
        image:cachedPath?convertFileSrc(cachedPath)+`?cache=${Date.now()}`:null,
        message:cachedPath?'Refresh queued • showing cached preview until replacement is ready':'Preview queued in background…'
      };
      applyPreviewState();
      void appLog('INFO','preview_refresh_queued',{source});
    }finally{button.disabled=false;}
  };
  retry.onclick=()=>void queueRefresh(retry);
  refresh.onclick=()=>void queueRefresh(refresh);
  open.onclick=()=>openSource(source,status,open);

  if(cached){
    desiredSource=null;
    previewState={
      source,phase:'success',kind,
      image:convertFileSrc(cached.path)+`?cache=1`,
      message:'Cached preview • ready'
    };
    applyPreviewState();
    void appLog('INFO','preview_cache_hit',{source,path:cached.path});
    return;
  }

  desiredSource=null;
  previewToken++;
  if(previewTimer){clearTimeout(previewTimer);previewTimer=null;}
  const queueStatus=detail.dataset.previewStatus||'pending';
  const queueError=detail.dataset.previewError||'';
  previewState={
    source,
    phase:queueStatus==='failed'?'failed':'pending',
    kind,
    image:null,
    message:queueStatus==='rendering'
      ?'Background preview is rendering…'
      :queueStatus==='failed'
        ?(queueError.includes('indicator OnInit failed (err 4802)')
          ?"Can't preview (indicator won't initialize)"
          :`Can't preview: ${queueError||'render failed after retry limit'}`)
        :'Preview queued in background…'
  };
  applyPreviewState();
}

function armButton(button,label,confirmLabel,ms=6000){
  const now=Date.now();
  const until=Number(button.dataset.confirmUntil||0);
  if(until>now)return true;
  button.dataset.confirmUntil=String(now+ms);
  button.textContent=confirmLabel;
  setTimeout(()=>{
    if(Number(button.dataset.confirmUntil||0)<=Date.now()&&document.body.contains(button)){
      button.dataset.confirmUntil='0';
      button.textContent=label;
    }
  },ms+200);
  return false;
}

function enhanceSourceRemoval(){
  const host=document.getElementById('sources');if(!host)return;
  for(const row of host.querySelectorAll('.source')){
    if(row.querySelector('.preview-remove-source'))continue;
    const source=row.querySelector('span')?.textContent?.trim();if(!source)continue;
    const button=document.createElement('button');
    button.className='btn preview-remove-source';
    button.textContent='Remove';
    button.style.marginLeft='auto';
    button.onclick=async e=>{
      e.stopPropagation();
      if(!armButton(button,'Remove','Click Again to Remove'))return;
      button.disabled=true;
      try{
        await execSidecar('binaries/mql-preview',['remove-source','--db',await dbPath(),'--source',source]);
        location.reload();
      }catch{
        button.disabled=false;
        button.dataset.confirmUntil='0';
        button.textContent='Remove';
      }
    };
    row.appendChild(button);
  }
}

async function clearApplication(button,status){
  const now=Date.now();
  if(clearConfirmUntil<=now){
    clearConfirmUntil=now+7000;
    button.textContent='Click Again to Clear Everything';
    status.textContent='Only this application’s local data will be cleared. Original MQ4/MQ5 files and source folders on your PC will NOT be deleted.';
    setTimeout(()=>{
      if(clearConfirmUntil<=Date.now()&&document.body.contains(button)){
        clearConfirmUntil=0;
        button.textContent='Clear Application / Start Fresh';
        status.textContent='Ready';
      }
    },7200);
    return;
  }

  clearConfirmUntil=0;
  button.disabled=true;
  button.textContent='Clearing Application…';
  status.textContent='Stopping preview work and removing local application data…';

  try{
    await invalidatePreview('application clear');
    const out=await execSidecar('binaries/mql-preview-control',['clear-app','--db',await dbPath()]);
    if(out.failed?.length)throw new Error(out.failed.join('; '));
    status.textContent='Application data cleared. Restarting with an empty library…';
    location.reload();
  }catch(e){
    button.disabled=false;
    button.textContent='Clear Application / Start Fresh';
    status.textContent=`Clear failed: ${e.message}`;
  }
}

function ensureClearCard(){
  const panel=document.querySelector('#settings .scan-panel');
  if(!panel)return;
  document.getElementById('archiveBackupCard')?.remove();
  document.getElementById('v57ArchiveCard')?.remove();
  document.getElementById('v5MetaTraderSettings')?.remove();
  if(document.getElementById('clearApplicationCard'))return;

  const card=document.createElement('div');
  card.className='diagnostic-card';
  card.id='clearApplicationCard';
  card.innerHTML=`<h3>Application Data</h3><p class="muted">Clear the MQL Indicator Library and start fresh. This removes the local library database, scan/review state, preview cache/runtime and diagnostics created by this app. It does not delete or modify your original indicator files or source folders on your PC.</p><button class="btn primary" id="clearApplicationBtn">Clear Application / Start Fresh</button><div class="status" id="clearApplicationStatus" style="margin-top:10px">Ready</div>`;
  panel.appendChild(card);

  const button=card.querySelector('#clearApplicationBtn');
  const status=card.querySelector('#clearApplicationStatus');
  button.onclick=()=>void clearApplication(button,status);
}

function startUiWatchdog(){
  let expected=performance.now()+500;
  setInterval(()=>{
    const now=performance.now();
    const stallMs=Math.round(now-expected);
    expected=now+500;
    if(stallMs>2000&&Date.now()-lastStallLog>5000){
      lastStallLog=Date.now();
      void appLog('WARN','ui_event_loop_stall',{
        stallMs,
        activeJob:activeJob?{jobId:activeJob.jobId,pid:activeJob.pid,source:activeJob.source}:null,
        desiredSource
      });
    }
  },500);
}

function boot(){
  const brand=document.querySelector('.brand small');
  if(brand)brand.textContent='0.5.14 • Evidence Engine v5 + Paired Runtime Discovery';
  ensureClearCard();
  enhanceSourceRemoval();
  startUiWatchdog();

  const observer=new MutationObserver(mutations=>{
    let detailNeedsCheck=false;
    let sourcesNeedCheck=false;

    for(const mutation of mutations){
      const target=mutation.target;
      if(target?.id==='detail'&&mutation.type==='attributes')detailNeedsCheck=true;
      if(target?.id==='detailBody'&&mutation.type==='childList'){
        const nodes=[...mutation.addedNodes,...mutation.removedNodes];
        const onlyOurPreview=nodes.length>0&&nodes.every(n=>n.nodeType!==1||n.id==='realMetaPreview'||n.closest?.('#realMetaPreview'));
        if(!onlyOurPreview)detailNeedsCheck=true;
      }
      if(target?.id==='sources'||target?.closest?.('#sources'))sourcesNeedCheck=true;
    }

    if(detailNeedsCheck)queueMicrotask(injectPreview);
    if(sourcesNeedCheck)queueMicrotask(enhanceSourceRemoval);
  });

  const root=document.getElementById('app');
  if(root)observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});
}

document.addEventListener('pointerdown',e=>{
  if(e.target.closest?.('#rows tr,#reviewRows tr')){
    previewState={source:null,phase:'idle',message:'',image:null,kind:null};
    cancelPreviewQuietly('new indicator selected');
  }
  if(e.target.closest?.('#closeDetail')){
    previewState={source:null,phase:'idle',message:'',image:null,kind:null};
    cancelPreviewQuietly('detail closed');
  }
  if(e.target.closest?.('[data-view="settings"]'))queueMicrotask(ensureClearCard);
  if(e.target.closest?.('[data-view="scan"]'))queueMicrotask(enhanceSourceRemoval);
},{capture:true});

document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    previewState={source:null,phase:'idle',message:'',image:null,kind:null};
    cancelPreviewQuietly('escape');
  }
});

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});
else boot();
