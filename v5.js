import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let activePreviewToken=0;

async function previewDb(){const base=await appDataDir();return join(base,'library.sqlite3');}
async function previewLog(level,event,details={}){try{await invoke('app_log',{dbPath:await previewDb(),level,event,details,durationMs:null});}catch{}}

async function previewCmd(args){
  const cmd=Command.sidecar('binaries/mql-preview',args);
  const out=await cmd.execute();
  const lines=(out.stdout||'').trim().split(/\r?\n/).filter(Boolean);
  let payload=null;
  for(let i=lines.length-1;i>=0;i--){try{payload=JSON.parse(lines[i]);break;}catch{}}
  if(out.code!==0||!payload?.ok)throw new Error(payload?.error||out.stderr||`Preview engine exited ${out.code}`);
  return payload;
}

function detailFilePath(){
  const detail=document.getElementById('detail');
  if(!detail)return null;
  for(const section of detail.querySelectorAll('.section')){
    const label=section.querySelector('.label')?.textContent?.trim();
    if(label==='File')return section.querySelector('.value')?.textContent?.trim()||null;
  }
  return null;
}

function closeDetail(){document.getElementById('detail')?.classList.remove('open');activePreviewToken++;}

document.addEventListener('pointerdown',e=>{
  const detail=document.getElementById('detail');
  if(!detail?.classList.contains('open'))return;
  if(detail.contains(e.target))return;
  if(e.target.closest?.('#rows tr, #reviewRows tr'))return;
  closeDetail();
},{capture:true});

document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDetail();});

async function renderRealPreview(source,status,img,retryButton=null){
  const token=++activePreviewToken;
  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const started=performance.now();
  if(retryButton)retryButton.disabled=true;
  status.textContent=`Rendering real ${kind} preview automatically…`;
  img.style.display='none';
  await previewLog('INFO','preview_start',{source,kind,automatic:true});
  try{
    const base=await appDataDir(); const outDir=await join(base,'previews');
    const result=await previewCmd(['render','--source',source,'--out',outDir]);
    if(token!==activePreviewToken||detailFilePath()!==source)return;
    img.src=convertFileSrc(result.image)+`?t=${Date.now()}`; img.style.display='block';
    status.textContent=`Real ${result.kind||kind} preview${result.cached?' • cached':''} • ${result.terminal?.install_dir||kind}`;
    await previewLog('INFO','preview_success',{source,kind:result.kind||kind,image:result.image,cached:!!result.cached,terminal:result.terminal?.terminal,elapsedMs:Math.round(performance.now()-started),automatic:true});
  }catch(e){
    if(token!==activePreviewToken)return;
    status.textContent=`Automatic preview failed: ${e.message}`;
    if(retryButton)retryButton.style.display='inline-flex';
    await previewLog('ERROR','preview_failed',{source,kind,error:e.message,elapsedMs:Math.round(performance.now()-started),automatic:true});
  }finally{if(retryButton)retryButton.disabled=false;}
}

async function openInMetaEditor(source,status,button){
  button.disabled=true;
  try{const out=await previewCmd(['open-source','--source',source]);status.textContent=`Opened in ${out.kind}: ${out.opened}`;await previewLog('INFO','preview_open_source',{source,kind:out.kind,opened:out.opened});}
  catch(e){status.textContent=`Could not open MetaTrader/MetaEditor: ${e.message}`;await previewLog('ERROR','preview_open_source_failed',{source,error:e.message});}
  finally{button.disabled=false;}
}

function injectPreviewCard(){
  const body=document.getElementById('detailBody'); if(!body)return;
  for(const section of [...body.querySelectorAll('.section')]){
    const label=section.querySelector('.label')?.textContent?.trim().toLowerCase()||'';
    if(label.includes('structural preview')||label.includes('schematic preview'))section.remove();
  }
  if(document.getElementById('v5RealPreview'))return;
  const source=detailFilePath(); if(!source)return;
  const section=document.createElement('div'); section.className='section'; section.id='v5RealPreview';
  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  section.innerHTML=`<div class="label">Real MetaTrader Preview</div>
    <div style="border:1px solid rgba(127,127,127,.22);border-radius:10px;padding:10px;margin-top:8px">
      <div class="muted" style="margin-bottom:9px">The real ${kind} preview renders automatically when this indicator opens. Cached previews load immediately when the source has not changed.</div>
      <div class="status" id="v5PreviewStatus">Preparing automatic preview…</div>
      <img id="v5PreviewImage" alt="MetaTrader indicator preview" style="display:none;width:100%;margin-top:10px;border-radius:8px;border:1px solid rgba(127,127,127,.25)"/>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
        <button class="btn" id="v5RetryPreview" style="display:none">Retry Preview</button>
        <button class="btn" id="v5OpenSource">Open in MetaTrader / MetaEditor</button>
      </div>
    </div>`;
  body.prepend(section);
  const status=section.querySelector('#v5PreviewStatus'),img=section.querySelector('#v5PreviewImage');
  const retry=section.querySelector('#v5RetryPreview'),open=section.querySelector('#v5OpenSource');
  retry.onclick=()=>{retry.style.display='none';renderRealPreview(source,status,img,retry);};
  open.onclick=()=>openInMetaEditor(source,status,open);
  renderRealPreview(source,status,img,retry);
}

function enhanceSourceRemoval(){
  const host=document.getElementById('sources'); if(!host)return;
  for(const row of host.querySelectorAll('.source')){
    if(row.querySelector('.v5-remove-source'))continue;
    const path=row.querySelector('span')?.textContent?.trim(); if(!path)continue;
    const btn=document.createElement('button'); btn.className='btn v5-remove-source'; btn.textContent='Remove'; btn.style.marginLeft='auto';
    btn.title='Remove this source and its indexed indicators from the library. Original files are not deleted.';
    btn.onclick=async e=>{
      e.stopPropagation();
      if(!confirm(`Remove this folder from the library?\n\n${path}\n\nThe original folder and files on disk will NOT be deleted.`))return;
      btn.disabled=true;
      try{
        const db=await previewDb(); const result=await previewCmd(['remove-source','--db',db,'--source',path]);
        await previewLog('INFO','source_removed',{source:path,removedIndicators:result.removed_indicators});
        location.reload();
      }catch(err){alert(`Could not remove source: ${err.message}`);btn.disabled=false;}
    };
    row.appendChild(btn);
  }
}

async function archiveAndFresh(button,status){
  if(!confirm('Archive the current library, diagnostics and preview cache, then start with a completely fresh library?\n\nOriginal indicator folders/files will NOT be deleted.'))return;
  button.disabled=true; status.textContent='Archiving current library…';
  try{
    const db=await previewDb(); const result=await previewCmd(['archive-reset','--db',db]);
    status.textContent=`Archive created: ${result.archive}`;
    await previewLog('INFO','archive_reset_complete',{archive:result.archive});
    setTimeout(()=>location.reload(),900);
  }catch(e){status.textContent=`Archive/reset failed: ${e.message}`;button.disabled=false;}
}

const observer=new MutationObserver(()=>{
  if(document.getElementById('detail')?.classList.contains('open'))setTimeout(injectPreviewCard,0);
  enhanceSourceRemoval();
});
const appRoot=document.getElementById('app'); if(appRoot)observer.observe(appRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});

async function enhanceSettings(){
  for(let i=0;i<50;i++){
    const brand=document.querySelector('.brand small'); if(brand)brand.textContent='0.5.2 • Evidence Engine v4 + Automatic MT4/MT5 Preview';
    const panel=document.querySelector('#settings .scan-panel');
    if(panel&&!document.getElementById('v5MetaTraderSettings')){
      const card=document.createElement('div'); card.className='diagnostic-card'; card.id='v5MetaTraderSettings';
      card.innerHTML=`<h3>MetaTrader Preview</h3><p class="muted">Real MT4 and MT5 previews now render automatically when you open an indicator. The selected source is staged and compiled inside MetaTrader's own preview workspace; successful previews are cached.</p><div id="v5TerminalStatus" class="status">Detecting MetaTrader…</div>
      <h3 style="margin-top:18px">Library Reset</h3><p class="muted">Archive the current database, diagnostics and preview cache to Downloads, then start with a clean library. Original indicator folders are never deleted.</p><button class="btn" id="v5ArchiveFresh">Archive Everything & Start Fresh</button><div class="status" id="v5ArchiveStatus" style="margin-top:9px">Ready</div>
      <h3 style="margin-top:18px">Learning Memory</h3><p class="muted">Human Verify/Correct decisions remain authoritative across rescans and are stored as reusable classification memory.</p><div id="v5MemoryStatus" class="status">Loading memory…</div>`;
      panel.insertBefore(card,panel.querySelector('h3:nth-last-of-type(1)')||null);
      const archiveBtn=card.querySelector('#v5ArchiveFresh'),archiveStatus=card.querySelector('#v5ArchiveStatus');
      archiveBtn.onclick=()=>archiveAndFresh(archiveBtn,archiveStatus);
      try{
        const detected=await previewCmd(['detect']); const terms=detected.terminals||[];
        document.getElementById('v5TerminalStatus').innerHTML=terms.length?terms.map(t=>`<div>${esc(t.kind)} • ${esc(t.install_dir)}${t.data_dir?' • data mapped':' • data folder not mapped yet'}</div>`).join(''):'No MetaTrader installation detected yet.';
      }catch(e){document.getElementById('v5TerminalStatus').textContent=`Detection error: ${e.message}`;}
      try{
        const base=await appDataDir(),db=await join(base,'library.sqlite3'); const m=await previewCmd(['memory-stats','--db',db]);
        document.getElementById('v5MemoryStatus').textContent=`${m.corrections} correction memories • ${m.verified} verified indicators • ${m.families} corrected families`;
      }catch(e){document.getElementById('v5MemoryStatus').textContent=`Memory status unavailable: ${e.message}`;}
      enhanceSourceRemoval();
      return;
    }
    await sleep(100);
  }
}

enhanceSettings();
enhanceSourceRemoval();
