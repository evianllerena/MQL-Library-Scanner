import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';

let token=0;
let timer=null;
let activeChild=null;
let clearConfirmUntil=0;

async function dbPath(){return join(await appDataDir(),'library.sqlite3');}
async function appLog(level,event,details={}){try{await invoke('app_log',{dbPath:await dbPath(),level,event,details,durationMs:null});}catch{}}

function parsePayload(text){
  const lines=String(text||'').trim().split(/\r?\n/).filter(Boolean);
  for(let i=lines.length-1;i>=0;i--){try{return JSON.parse(lines[i]);}catch{}}
  return null;
}

async function execSidecar(name,args){
  const out=await Command.sidecar(name,args).execute();
  const payload=parsePayload(out.stdout);
  if(out.code!==0||!payload?.ok)throw new Error(payload?.error||out.stderr||`${name} exited ${out.code}`);
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

async function cancelActive(reason){
  const child=activeChild;
  if(!child)return;
  activeChild=null;
  try{
    await execSidecar('binaries/mql-preview-control',['cancel','--pid',String(child.pid)]);
    await appLog('INFO','preview_cancelled',{pid:child.pid,reason});
  }catch(e){
    try{await child.kill();}catch{}
    await appLog('WARN','preview_cancel_failed',{pid:child.pid,reason,error:String(e)});
  }
}

function invalidatePreview(reason){
  token++;
  if(timer){clearTimeout(timer);timer=null;}
  void cancelActive(reason);
}

async function spawnRender(args,myToken){
  const cmd=Command.sidecar('binaries/mql-preview',args);
  let stdout='',stderr='';
  cmd.stdout.on('data',data=>{stdout+=String(data)+'\n';});
  cmd.stderr.on('data',data=>{stderr+=String(data)+'\n';});
  const closed=new Promise((resolve,reject)=>{
    cmd.on('close',data=>resolve(data));
    cmd.on('error',error=>reject(error));
  });
  const child=await cmd.spawn();
  if(myToken!==token){
    try{await execSidecar('binaries/mql-preview-control',['cancel','--pid',String(child.pid)]);}catch{try{await child.kill();}catch{}}
    throw new Error('Preview cancelled');
  }
  activeChild=child;
  const closeData=await closed;
  if(activeChild?.pid===child.pid)activeChild=null;
  const payload=parsePayload(stdout);
  if(closeData.code!==0||!payload?.ok)throw new Error(payload?.error||stderr||`Preview engine exited ${closeData.code}`);
  return payload;
}

function schedulePreview(source,status,img,retry,delay=900){
  invalidatePreview('preview superseded');
  const myToken=token;
  status.textContent='Preparing automatic preview…';
  timer=setTimeout(async()=>{
    timer=null;
    if(myToken!==token||detailSource()!==source)return;
    const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
    const started=performance.now();
    retry.disabled=true;
    status.textContent=`Rendering real ${kind} preview…`;
    img.style.display='none';
    await appLog('INFO','preview_start',{source,kind,automatic:true});
    try{
      const result=await spawnRender(['render','--source',source,'--out',await join(await appDataDir(),'previews')],myToken);
      if(myToken!==token||detailSource()!==source)return;
      img.src=convertFileSrc(result.image)+`?t=${Date.now()}`;
      img.style.display='block';
      retry.style.display='none';
      status.textContent=`Real ${result.kind||kind} preview • ${result.terminal?.install_dir||kind}`;
      await appLog('INFO','preview_success',{source,kind:result.kind||kind,image:result.image,elapsedMs:Math.round(performance.now()-started),automatic:true});
    }catch(e){
      if(myToken!==token)return;
      const msg=String(e?.message||e);
      status.textContent=msg.startsWith('Indicator compile failed')?msg:`Automatic preview failed: ${msg}`;
      retry.style.display='inline-flex';
      await appLog('ERROR','preview_failed',{source,kind,error:msg,elapsedMs:Math.round(performance.now()-started),automatic:true});
    }finally{retry.disabled=false;}
  },delay);
}

async function openSource(source,status,button){
  button.disabled=true;
  try{const out=await execSidecar('binaries/mql-preview',['open-source','--source',source]);status.textContent=`Opened in ${out.kind}: ${out.opened}`;}
  catch(e){status.textContent=`Could not open MetaTrader/MetaEditor: ${e.message}`;}
  finally{button.disabled=false;}
}

function injectPreview(){
  const detail=document.getElementById('detail');
  const body=document.getElementById('detailBody');
  if(!detail?.classList.contains('open')||!body)return;
  const source=detailSource();
  if(!source)return;

  const existing=document.getElementById('realMetaPreview');
  if(existing?.dataset.source===source)return;
  existing?.remove();
  for(const old of [...body.querySelectorAll('#v5RealPreview,#v57RealPreview')])old.remove();

  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const section=document.createElement('div');
  section.className='section';
  section.id='realMetaPreview';
  section.dataset.source=source;
  section.innerHTML=`<div class="label">Real MetaTrader Preview</div><div style="border:1px solid rgba(127,127,127,.22);border-radius:10px;padding:10px;margin-top:8px"><div class="muted" style="margin-bottom:9px">Real ${kind} rendering starts after the selection settles. Selecting another indicator cancels the previous preview process tree.</div><div class="status" id="previewStatus">Preparing automatic preview…</div><img id="previewImage" alt="MetaTrader indicator preview" style="display:none;width:100%;margin-top:10px;border-radius:8px;border:1px solid rgba(127,127,127,.25)"/><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="btn" id="retryPreview" style="display:none">Retry Preview</button><button class="btn" id="openPreviewSource">Open in MetaTrader / MetaEditor</button></div></div>`;
  body.prepend(section);

  const status=section.querySelector('#previewStatus');
  const img=section.querySelector('#previewImage');
  const retry=section.querySelector('#retryPreview');
  const open=section.querySelector('#openPreviewSource');
  retry.onclick=()=>schedulePreview(source,status,img,retry,0);
  open.onclick=()=>openSource(source,status,open);
  schedulePreview(source,status,img,retry);
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
  invalidatePreview('application clear');

  try{
    await new Promise(resolve=>setTimeout(resolve,250));
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

function boot(){
  const brand=document.querySelector('.brand small');
  if(brand)brand.textContent='0.5.8 • Evidence Engine v4 + Cancellable MT4/MT5 Preview';
  ensureClearCard();
  enhanceSourceRemoval();

  const observer=new MutationObserver(mutations=>{
    let detailNeedsCheck=false;
    let sourcesNeedCheck=false;

    for(const mutation of mutations){
      const target=mutation.target;
      if(target?.id==='detail'&&mutation.type==='attributes')detailNeedsCheck=true;
      if(target?.id==='detailBody'&&mutation.type==='childList'){
        const onlyOurPreview=[...mutation.addedNodes,...mutation.removedNodes].every(n=>n.nodeType!==1||n.id==='realMetaPreview'||n.closest?.('#realMetaPreview'));
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
  if(e.target.closest?.('#rows tr,#reviewRows tr'))invalidatePreview('new indicator selected');
  if(e.target.closest?.('#closeDetail'))invalidatePreview('detail closed');
  if(e.target.closest?.('[data-view="settings"]'))queueMicrotask(ensureClearCard);
  if(e.target.closest?.('[data-view="scan"]'))queueMicrotask(enhanceSourceRemoval);
},{capture:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')invalidatePreview('escape');});

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});
else boot();
