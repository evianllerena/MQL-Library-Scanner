import { Command } from '@tauri-apps/plugin-shell';
import { appDataDir, join } from '@tauri-apps/api/path';
import { convertFileSrc, invoke } from '@tauri-apps/api/core';

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[c]));

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

function closeDetail(){document.getElementById('detail')?.classList.remove('open');}

document.addEventListener('pointerdown',e=>{
  const detail=document.getElementById('detail');
  if(!detail?.classList.contains('open'))return;
  if(detail.contains(e.target))return;
  if(e.target.closest?.('#rows tr, #reviewRows tr'))return;
  closeDetail();
},{capture:true});

document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDetail();});

async function renderRealPreview(source,status,img,button){
  const kind=/\.(mq5|ex5)$/i.test(source)?'MT5':'MT4';
  const started=performance.now();
  button.disabled=true; status.textContent=`Starting real ${kind} preview…`; img.style.display='none';
  await previewLog('INFO','preview_start',{source,kind});
  try{
    const base=await appDataDir(); const outDir=await join(base,'previews');
    const result=await previewCmd(['render','--source',source,'--out',outDir]);
    img.src=convertFileSrc(result.image)+`?t=${Date.now()}`; img.style.display='block';
    status.textContent=`Real ${result.renderer||kind} chart preview • ${result.terminal?.install_dir||kind}`;
    await previewLog('INFO','preview_success',{source,kind:result.renderer||kind,image:result.image,terminal:result.terminal?.terminal,elapsedMs:Math.round(performance.now()-started)});
  }catch(e){
    status.textContent=`Preview unavailable: ${e.message}`;
    await previewLog('ERROR','preview_failed',{source,kind,error:e.message,elapsedMs:Math.round(performance.now()-started)});
  }finally{button.disabled=false;}
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
      <div class="muted" style="margin-bottom:9px">Runs the actual ${kind} indicator in MetaTrader and captures the real chart. MT4 uses a disposable preview template; MT5 uses native indicator attachment.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" id="v5RenderPreview">Render Real ${kind} Preview</button>
        <button class="btn" id="v5OpenSource">Open in MetaTrader / MetaEditor</button>
      </div>
      <div class="status" id="v5PreviewStatus" style="margin-top:9px">Ready</div>
      <img id="v5PreviewImage" alt="MetaTrader indicator preview" style="display:none;width:100%;margin-top:10px;border-radius:8px;border:1px solid rgba(127,127,127,.25)"/>
    </div>`;
  body.prepend(section);
  const status=section.querySelector('#v5PreviewStatus'),img=section.querySelector('#v5PreviewImage');
  const render=section.querySelector('#v5RenderPreview'),open=section.querySelector('#v5OpenSource');
  if(render)render.onclick=()=>renderRealPreview(source,status,img,render);
  if(open)open.onclick=()=>openInMetaEditor(source,status,open);
}

const observer=new MutationObserver(()=>{if(document.getElementById('detail')?.classList.contains('open'))setTimeout(injectPreviewCard,0);});
const appRoot=document.getElementById('app'); if(appRoot)observer.observe(appRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});

async function enhanceSettings(){
  for(let i=0;i<50;i++){
    const brand=document.querySelector('.brand small'); if(brand)brand.textContent='0.5.1 • Evidence Engine v4 + MT4/MT5 Real Preview';
    const panel=document.querySelector('#settings .scan-panel');
    if(panel&&!document.getElementById('v5MetaTraderSettings')){
      const card=document.createElement('div'); card.className='diagnostic-card'; card.id='v5MetaTraderSettings';
      card.innerHTML=`<h3>MetaTrader Preview</h3><p class="muted">0.5.1 detects installed MT4 and MT5 terminals and renders real chart previews for both platforms. MT4 uses a generated temporary template and capture script; MT5 uses native indicator attachment.</p><div id="v5TerminalStatus" class="status">Detecting MetaTrader…</div><h3 style="margin-top:18px">Learning Memory</h3><p class="muted">Human Verify/Correct decisions remain authoritative across rescans and are stored as reusable classification memory.</p><div id="v5MemoryStatus" class="status">Loading memory…</div>`;
      panel.insertBefore(card,panel.querySelector('h3:nth-last-of-type(1)')||null);
      try{
        const detected=await previewCmd(['detect']);
        const terms=detected.terminals||[];
        document.getElementById('v5TerminalStatus').innerHTML=terms.length?terms.map(t=>`<div>${esc(t.kind)} • ${esc(t.install_dir)}${t.data_dir?' • data mapped':' • data folder not mapped yet'}</div>`).join(''):'No MetaTrader installation detected yet.';
      }catch(e){document.getElementById('v5TerminalStatus').textContent=`Detection error: ${e.message}`;}
      try{
        const base=await appDataDir(),db=await join(base,'library.sqlite3');
        const m=await previewCmd(['memory-stats','--db',db]);
        document.getElementById('v5MemoryStatus').textContent=`${m.corrections} correction memories • ${m.verified} verified indicators • ${m.families} corrected families`;
      }catch(e){document.getElementById('v5MemoryStatus').textContent=`Memory status unavailable: ${e.message}`;}
      return;
    }
    await sleep(100);
  }
}

enhanceSettings();
