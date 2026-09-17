import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let previewToken=0;
let previewTimer=null;
let activeChild=null;

async function dbPath(){const base=await appDataDir();return join(base,'library.sqlite3');}
async function appLog(level,event,details={}){try{await invoke('app_log',{dbPath:await dbPath(),level,event,details,durationMs:null});}catch{}}

async function execSidecar(name,args){
  const out=await Command.sidecar(name,args).execute();
  const lines=(out.stdout||'').trim().split(/\r?\n/).filter(Boolean);
  let payload=null;
  for(let i=lines.length-1;i>=0;i--){try{payload=JSON.parse(lines[i]);break;}catch{}}
  if(out.code!==0||!payload?.ok)throw new Error(payload?.error||out.stderr||`${name} exited ${out.code}`);
  return payload;
}

function detailFilePath(){
  const detail=document.getElementById('detail');
  if(!detail)return null;
  for(const section of detail.querySelectorAll('.section')){
    if(section.querySelector('.label')?.textContent?.trim()==='File')return section.querySelector('.value')?.textContent?.trim()||null;
  }
  return null;
}

async function cancelActivePreview(reason='selection changed'){
  const current=activeChild;
  if(!current)return;
  activeChild=null;
  try{
    await execSidecar('binaries/mql-preview-control',['cancel','--pid',String(current.pid)]);
    await appLog('INFO','preview_cancelled',{pid:current.pid,reason});
  }catch(e){
    try{await current.kill();}catch{}
    await appLog('WARN','preview_cancel_failed',{pid:current.pid,reason,error:String(e)});
  }
}

function closeDetail(){
  document.getElementById('detail')?.classList.remove('open');
  previewToken++;
  if(previewTimer){clearTimeout(previewTimer);previewTimer=null;}
  cancelActivePreview('detail closed');
}

document.addEventListener('pointerdown',e=>{
  const detail=document.getElementById('detail');
  if(detail?.classList.contains('open')&&!detail.contains(e.target)&&!e.target.closest?.('#rows tr, #reviewRows tr'))closeDetail();
  if(e.target.closest?.('#rows tr, #reviewRows tr'))cancelActivePreview('new indicator selected');
},{capture:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDetail();});

function parsePayload(stdout){
  const lines=(stdout||'').trim().split(/\r?\n/).filter(Boolean);
  for(let i=lines.length-1;i>=0;i--){try{return JSON.parse(lines[i]);}catch{}}
  return null;
}

async function runRender(args,token){
  const cmd=Command.sidecar('binaries/mql-preview',args);
  let stdout='',stderr='';
  cmd.stdout.on('data',d=>{stdout+=String(d)+'\n';});
  cmd.stderr.on('data',d=>{stderr+=String(d)+'\n';});
  const done=new Promise((resolve,reject)=>{
    cmd.on('close',data=>resolve({code:data.code,stdout,stderr}));
    cmd.on('error',error=>reject(new Error(String(error))));
  });
  const child=await cmd.spawn();
  if(token!==previewToken){
    try{await execSidecar('binaries/mql-preview-control',['cancel','--pid',String(child.pid)]);}catch{try{await child.kill();}catch{}}
    throw new Error('Preview cancelled');
  }
  activeChild=child;
  const out=await done;
  if(activeChild?.pid===child.pid)activeChild=null;
  const payload=parsePayload(out.stdout);
  if(out.code!==0||!payload?.ok)throw new Error(payload?.error||out.stderr||`Preview engine exited ${out.code}`);
  return payload;
}

function schedulePreview(source,status,img,retry,delay=900){
  const token=++previewToken;
  if(previewTimer)clearTimeout(previewTimer);
  status.textContent='Preparing automatic preview…';
  cancelActivePreview('preview superseded');
  previewTimer=setTimeout(async()=>{
    previewTimer=null;
    if(token!==previewToken||detailFilePath()!==source)return;
    const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
    const started=performance.now();
    retry.disabled=true;
    status.textContent=`Rendering real ${kind} preview…`;
    img.style.display='none';
    await appLog('INFO','preview_start',{source,kind,automatic:true});
    try{
      const base=await appDataDir(),outDir=await join(base,'previews');
      const result=await runRender(['render','--source',source,'--out',outDir],token);
      if(token!==previewToken||detailFilePath()!==source)return;
      img.src=convertFileSrc(result.image)+`?t=${Date.now()}`;img.style.display='block';
      status.textContent=`Real ${result.kind||kind} preview • ${result.terminal?.install_dir||kind}`;
      await appLog('INFO','preview_success',{source,kind:result.kind||kind,image:result.image,elapsedMs:Math.round(performance.now()-started),automatic:true});
    }catch(e){
      if(token!==previewToken)return;
      const msg=String(e?.message||e);
      status.textContent=msg.startsWith('Indicator compile failed')?msg:`Automatic preview failed: ${msg}`;
      retry.style.display='inline-flex';
      await appLog('ERROR','preview_failed',{source,kind,error:msg,elapsedMs:Math.round(performance.now()-started),automatic:true});
    }finally{
      retry.disabled=false;
    }
  },delay);
}

async function openSource(source,status,button){
  button.disabled=true;
  try{const out=await execSidecar('binaries/mql-preview',['open-source','--source',source]);status.textContent=`Opened in ${out.kind}: ${out.opened}`;}
  catch(e){status.textContent=`Could not open MetaTrader/MetaEditor: ${e.message}`;}
  finally{button.disabled=false;}
}

function injectPreviewCard(){
  const body=document.getElementById('detailBody');if(!body)return;
  for(const section of [...body.querySelectorAll('.section')]){
    const label=section.querySelector('.label')?.textContent?.trim().toLowerCase()||'';
    if(label.includes('structural preview')||label.includes('schematic preview')||section.id==='v5RealPreview')section.remove();
  }
  if(document.getElementById('v57RealPreview'))return;
  const source=detailFilePath();if(!source)return;
  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const section=document.createElement('div');section.className='section';section.id='v57RealPreview';
  section.innerHTML=`<div class="label">Real MetaTrader Preview</div><div style="border:1px solid rgba(127,127,127,.22);border-radius:10px;padding:10px;margin-top:8px"><div class="muted" style="margin-bottom:9px">The preview starts after selection settles. Changing indicators cancels the previous MetaTrader job instead of waiting behind it.</div><div class="status" id="v57PreviewStatus">Preparing automatic preview…</div><img id="v57PreviewImage" alt="MetaTrader indicator preview" style="display:none;width:100%;margin-top:10px;border-radius:8px;border:1px solid rgba(127,127,127,.25)"/><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="btn" id="v57RetryPreview" style="display:none">Retry Preview</button><button class="btn" id="v57OpenSource">Open in MetaTrader / MetaEditor</button></div></div>`;
  body.prepend(section);
  const status=section.querySelector('#v57PreviewStatus'),img=section.querySelector('#v57PreviewImage'),retry=section.querySelector('#v57RetryPreview'),open=section.querySelector('#v57OpenSource');
  retry.onclick=()=>{retry.style.display='none';schedulePreview(source,status,img,retry,0);};
  open.onclick=()=>openSource(source,status,open);
  schedulePreview(source,status,img,retry);
}

function enhanceSourceRemoval(){
  const host=document.getElementById('sources');if(!host)return;
  for(const row of host.querySelectorAll('.source')){
    if(row.querySelector('.v57-remove-source'))continue;
    const path=row.querySelector('span')?.textContent?.trim();if(!path)continue;
    const btn=document.createElement('button');btn.className='btn v57-remove-source';btn.textContent='Remove';btn.style.marginLeft='auto';
    btn.onclick=async e=>{e.stopPropagation();if(!confirm(`Remove this folder from the library?\n\n${path}\n\nOriginal files will NOT be deleted.`))return;btn.disabled=true;try{const out=await execSidecar('binaries/mql-preview',['remove-source','--db',await dbPath(),'--source',path]);await appLog('INFO','source_removed',{source:path,removedIndicators:out.removed_indicators});location.reload();}catch(err){alert(`Could not remove source: ${err.message}`);btn.disabled=false;}};
    row.appendChild(btn);
  }
}

async function createArchive(button,status){
  button.disabled=true;status.textContent='Creating backup archive…';
  try{const out=await execSidecar('binaries/mql-preview-control',['archive','--db',await dbPath()]);status.textContent=`Backup created: ${out.archive}`;await appLog('INFO','archive_created',{archive:out.archive,files:out.files});}
  catch(e){status.textContent=`Archive failed: ${e.message}`;await appLog('ERROR','archive_failed',{error:String(e)});}
  finally{button.disabled=false;}
}

async function ensureSettings(){
  const panel=document.querySelector('#settings .scan-panel');if(!panel)return;
  const brand=document.querySelector('.brand small');if(brand)brand.textContent='0.5.7 • Evidence Engine v4 + Cancellable MT4/MT5 Preview';
  if(document.getElementById('v57ArchiveCard'))return;
  document.getElementById('v5MetaTraderSettings')?.remove();
  const card=document.createElement('div');card.className='diagnostic-card';card.id='v57ArchiveCard';
  card.innerHTML=`<h3>Library Archive</h3><p class="muted">Create a backup ZIP of the library database, diagnostics and saved preview images. The temporary MetaTrader preview runtime is excluded because it can be rebuilt.</p><button class="btn primary" id="v57ArchiveBtn">Create Archive Backup</button><div class="status" id="v57ArchiveStatus" style="margin-top:10px">Ready</div><h3 style="margin-top:18px">MetaTrader Preview</h3><div id="v57TerminalStatus" class="status">Detecting MetaTrader…</div><h3 style="margin-top:18px">Learning Memory</h3><div id="v57MemoryStatus" class="status">Loading memory…</div>`;
  panel.appendChild(card);
  const btn=card.querySelector('#v57ArchiveBtn'),status=card.querySelector('#v57ArchiveStatus');btn.onclick=()=>createArchive(btn,status);
  try{const detected=await execSidecar('binaries/mql-preview',['detect']);const terms=detected.terminals||[];card.querySelector('#v57TerminalStatus').innerHTML=terms.length?terms.map(t=>`<div>${esc(t.kind)} • ${esc(t.install_dir)}${t.data_dir?' • data mapped':' • data folder not mapped'}</div>`).join(''):'No MetaTrader installation detected.';}catch(e){card.querySelector('#v57TerminalStatus').textContent=`Detection error: ${e.message}`;}
  try{const m=await execSidecar('binaries/mql-preview',['memory-stats','--db',await dbPath()]);card.querySelector('#v57MemoryStatus').textContent=`${m.corrections} correction memories • ${m.verified} verified indicators • ${m.families} corrected families`;}catch(e){card.querySelector('#v57MemoryStatus').textContent=`Memory status unavailable: ${e.message}`;}
}

const observer=new MutationObserver(()=>{
  if(document.getElementById('detail')?.classList.contains('open'))setTimeout(injectPreviewCard,0);
  enhanceSourceRemoval();ensureSettings();
});
const root=document.getElementById('app');if(root)observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});

for(let i=0;i<50;i++){if(document.querySelector('#settings .scan-panel'))break;await sleep(100);}
ensureSettings();enhanceSourceRemoval();
