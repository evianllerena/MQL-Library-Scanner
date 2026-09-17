import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';

let token=0;
let timer=null;
let activeChild=null;
let settingsLoaded=false;

const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

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
  if(myToken!==token){try{await child.kill();}catch{};throw new Error('Preview cancelled');}
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
  for(const old of [...body.querySelectorAll('#v5RealPreview,#v57RealPreview,#realMetaPreview')])old.remove();
  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const section=document.createElement('div');
  section.className='section';section.id='realMetaPreview';
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

async function createArchive(button,status){
  button.disabled=true;status.textContent='Creating backup archive…';
  try{
    const out=await execSidecar('binaries/mql-preview-control',['archive','--db',await dbPath()]);
    status.textContent=`Backup created: ${out.archive}`;
    await appLog('INFO','archive_created',{archive:out.archive,files:out.files});
  }catch(e){
    status.textContent=`Archive failed: ${e.message}`;
    await appLog('ERROR','archive_failed',{error:String(e)});
  }finally{button.disabled=false;}
}

function ensureArchiveCard(){
  const panel=document.querySelector('#settings .scan-panel');
  if(!panel||document.getElementById('archiveBackupCard'))return;
  document.getElementById('v5MetaTraderSettings')?.remove();
  document.getElementById('v57ArchiveCard')?.remove();
  const card=document.createElement('div');
  card.className='diagnostic-card';card.id='archiveBackupCard';
  card.innerHTML=`<h3>Library Archive</h3><p class="muted">Create a backup ZIP of the library database, diagnostics and saved preview images. This does not reset or delete the library. The temporary MetaTrader preview runtime is excluded because it can be rebuilt.</p><button class="btn primary" id="archiveBackupBtn">Create Archive Backup</button><div class="status" id="archiveBackupStatus" style="margin-top:10px">Ready</div><h3 style="margin-top:18px">MetaTrader Preview</h3><div class="status" id="previewTerminalStatus">Open Settings to refresh MetaTrader status.</div>`;
  panel.appendChild(card);
  const button=card.querySelector('#archiveBackupBtn');
  const status=card.querySelector('#archiveBackupStatus');
  button.onclick=()=>createArchive(button,status);
  settingsLoaded=false;
}

async function refreshSettingsStatus(){
  ensureArchiveCard();
  if(settingsLoaded)return;
  settingsLoaded=true;
  const box=document.getElementById('previewTerminalStatus');
  if(!box)return;
  box.textContent='Detecting MetaTrader…';
  try{
    const out=await execSidecar('binaries/mql-preview',['detect']);
    const terms=out.terminals||[];
    box.innerHTML=terms.length?terms.map(t=>`<div>${esc(t.kind)} • ${esc(t.install_dir)}${t.data_dir?' • data mapped':' • data folder not mapped'}</div>`).join(''):'No MetaTrader installation detected.';
  }catch(e){box.textContent=`Detection error: ${e.message}`;}
}

function enhanceSourceRemoval(){
  const host=document.getElementById('sources');if(!host)return;
  for(const row of host.querySelectorAll('.source')){
    if(row.querySelector('.preview-remove-source'))continue;
    const source=row.querySelector('span')?.textContent?.trim();if(!source)continue;
    const button=document.createElement('button');button.className='btn preview-remove-source';button.textContent='Remove';button.style.marginLeft='auto';
    button.onclick=async e=>{
      e.stopPropagation();
      if(!confirm(`Remove this folder from the library?\n\n${source}\n\nOriginal files will NOT be deleted.`))return;
      button.disabled=true;
      try{await execSidecar('binaries/mql-preview',['remove-source','--db',await dbPath(),'--source',source]);location.reload();}
      catch(err){alert(`Could not remove source: ${err.message}`);button.disabled=false;}
    };
    row.appendChild(button);
  }
}

function boot(){
  const brand=document.querySelector('.brand small');
  if(brand)brand.textContent='0.5.7 • Evidence Engine v4 + Cancellable MT4/MT5 Preview';
  ensureArchiveCard();enhanceSourceRemoval();
  const observer=new MutationObserver(()=>{
    ensureArchiveCard();enhanceSourceRemoval();
    if(document.getElementById('detail')?.classList.contains('open'))queueMicrotask(injectPreview);
  });
  const root=document.getElementById('app');
  if(root)observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});
}

// Capture navigation/selection changes before the base UI swaps detail content.
document.addEventListener('pointerdown',e=>{
  if(e.target.closest?.('#rows tr,#reviewRows tr'))invalidatePreview('new indicator selected');
  if(e.target.closest?.('#closeDetail'))invalidatePreview('detail closed');
  if(e.target.closest?.('[data-view="settings"]'))void refreshSettingsStatus();
},{capture:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')invalidatePreview('escape');});

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});
else boot();
