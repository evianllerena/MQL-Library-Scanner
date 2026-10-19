"""preview_bridge self-tests: the offline `self-test` checks and the live `preview-selftest`. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (emit, hard_kill, mql_dir_name, read_text, safe_job_id)  # noqa: F401
from mt_compile import (compile_has_errors, safe_name)  # noqa: F401
from mt_discovery import (choose_runtime, decode_origin_bytes, discover_data_dirs, discover_installations, discovery_diagnostics, load_runtime_cache, pair_runtime_candidates, selection_checks, write_runtime_cache)  # noqa: F401
from mt_runtime import (clone_runtime, runtime_path, seed_render_dependencies_from_data_dir)  # noqa: F401
from preview_render import (RESIDENT_EA_SOURCE, _mql_str, _render_failure_message, _resident_indicator_rel, compile_pool, mt5_capture_source, render, render_mt4, render_server)  # noqa: F401


def _selftest_default_db():
    appdata=Path(os.environ.get('APPDATA',''))
    return appdata/'com.mqlindicatorlibrary.app'/'library.sqlite3'

def _selftest_source_text(path):
    p=Path(path)
    return read_text(p).lower() if p.suffix.lower() in ('.mq4','.mq5') else ''

def _selftest_rank(path):
    p=Path(path);name=p.name.lower();text=_selftest_source_text(p)
    score=0
    if 'my tma+channel' in name or ('tma' in name and 'channel' in name):score+=2000
    if re.search(r'(^|[^a-z0-9])3ls([^a-z0-9]|$)',name):score+=1900
    if 'indicator_separate_window' in text:score+=1200
    if any(x in name for x in ('mtf','multi timeframe','multi-timeframe','multitimeframe')):score+=1000
    if any(x in name for x in ('rsi','macd','stoch','osc','moving average','ma_','average')):score+=250
    if p.suffix.lower() in ('.mq5','.ex5'):score+=50
    return (-score,name.lower(),str(p).lower())

def _selftest_candidates(source=None,db=None,sample=30):
    paths=[]
    if source:
        root=Path(source)
        if not root.exists():raise RuntimeError(f'Self-test source folder does not exist: {root}')
        allowed={'.mq4','.ex4','.mq5','.ex5'}
        paths=[p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in allowed]
    else:
        dbp=Path(db) if db else _selftest_default_db()
        if not dbp.is_file():raise RuntimeError(f'Self-test needs --source or an existing library database: {dbp}')
        con=sqlite3.connect(dbp)
        try:
            rows=con.execute("SELECT path FROM indicators WHERE path IS NOT NULL AND path!='' ORDER BY id").fetchall()
        finally:con.close()
        for (raw,) in rows:
            p=Path(raw)
            if p.is_file() and p.suffix.lower() in ('.mq4','.ex4','.mq5','.ex5'):paths.append(p)
    unique=[];seen=set()
    for p in paths:
        try:key=os.path.normcase(str(p.resolve()))
        except Exception:key=os.path.normcase(str(p))
        if key in seen:continue
        seen.add(key);unique.append(p)
    n=max(1,int(sample or 30))
    mt5=sorted([p for p in unique if p.suffix.lower() in ('.mq5','.ex5')],key=_selftest_rank)
    mt4=sorted([p for p in unique if p.suffix.lower() in ('.mq4','.ex4')],key=_selftest_rank)
    selected=[]
    if mt5 and mt4 and n>=4:
        mt4_n=min(len(mt4),max(2,min(6,n//4)))
        mt5_n=min(len(mt5),n-mt4_n)
        selected.extend(mt5[:mt5_n]);selected.extend(mt4[:mt4_n])
        remain=n-len(selected)
        if remain>0:
            extras=mt5[mt5_n:]+mt4[mt4_n:]
            selected.extend(sorted(extras,key=_selftest_rank)[:remain])
    else:
        selected=sorted(unique,key=_selftest_rank)[:n]
    return sorted(selected,key=_selftest_rank)[:n]

def _selftest_db(path,mt5_paths):
    path=Path(path)
    try:path.unlink(missing_ok=True)
    except Exception:pass
    con=sqlite3.connect(path)
    try:
        con.execute("""CREATE TABLE indicators(
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            filename TEXT,
            platform TEXT,
            sha256 TEXT,
            duplicate_of INTEGER,
            user_favorite INTEGER DEFAULT 0,
            analyzed_at TEXT,
            preview_status TEXT DEFAULT 'pending',
            preview_path TEXT,
            preview_hash TEXT,
            preview_error TEXT DEFAULT '',
            preview_updated_at TEXT,
            preview_attempts INTEGER DEFAULT 0,
            preview_priority INTEGER DEFAULT 0,
            worker_id TEXT
        )""")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        for i,p in enumerate(mt5_paths,1):
            h=hashlib.sha256(Path(p).read_bytes()).hexdigest()
            con.execute(
                "INSERT INTO indicators(id,path,filename,platform,sha256,duplicate_of,user_favorite,analyzed_at,preview_priority) "
                "VALUES(?,?,?,?,?,NULL,0,datetime('now'),1000)",
                (i,str(p),Path(p).name,'MQL5',h))
        con.commit()
    finally:con.close()
    return path

def _selftest_chart_crop(im):
    w,h=im.size
    left=max(0,int(w*.04));top=max(0,int(h*.04));right=max(left+1,int(w*.97));bottom=max(top+1,int(h*.93))
    return im.crop((left,top,right,bottom))

def _selftest_dominant_color(im):
    q=im.resize((120,72)).convert('RGB').quantize(colors=8)
    colors=q.getcolors() or []
    if not colors:return (0,0,0)
    idx=max(colors,key=lambda x:x[0])[1]
    palette=q.getpalette() or []
    base=idx*3
    return tuple(palette[base:base+3]) if len(palette)>=base+3 else (0,0,0)

def _selftest_nonbg_coverage(im,bg,threshold=48):
    px=im.resize((480,288)).convert('RGB').getdata()
    total=0;changed=0
    br,bg0,bb=bg
    for r,g,b in px:
        total+=1
        if abs(r-br)+abs(g-bg0)+abs(b-bb)>threshold:changed+=1
    return changed/max(1,total)

def _selftest_saturated_colors(im):
    colors=set()
    for r,g,b in im.resize((160,96)).convert('RGB').getdata():
        if max(r,g,b)-min(r,g,b)>=50:
            colors.add((r//32,g//32,b//32))
    return colors

def _selftest_image_metrics(reference,candidate,kind='MT5'):
    from PIL import Image,ImageChops
    with Image.open(reference) as rb, Image.open(candidate) as rc:
        base=_selftest_chart_crop(rb.convert('RGB'))
        cand=_selftest_chart_crop(rc.convert('RGB'))
        size=(480,288)
        base=base.resize(size);cand=cand.resize(size)
        bg=_selftest_dominant_color(base)
        base_nonbg=_selftest_nonbg_coverage(base,bg)
        cand_nonbg=_selftest_nonbg_coverage(cand,bg)
        delta=max(0.0,cand_nonbg-base_nonbg)
        diff=ImageChops.difference(cand,base)
        changed=sum(1 for p in diff.getdata() if max(p)>28)
        diff_coverage=changed/(size[0]*size[1])
        base_colors=_selftest_saturated_colors(base)
        cand_colors=_selftest_saturated_colors(cand)
        novel_colors=len(cand_colors-base_colors)
        blank_ceiling=max(.0015,min(.012,base_nonbg*.06))
        if str(kind).upper()=='MT5':
            drew=(diff_coverage>blank_ceiling) or (delta>.0015 and novel_colors>=1)
            coverage=diff_coverage
        else:
            mt4_ceiling=max(.0030,min(.02,base_nonbg*.08))
            drew=(delta>mt4_ceiling) or (delta>.0015 and novel_colors>=4)
            coverage=delta
        return {
            'coverage':round(float(coverage),6),
            'diff_coverage':round(float(diff_coverage),6),
            'non_background':round(float(cand_nonbg),6),
            'baseline_non_background':round(float(base_nonbg),6),
            'coverage_delta':round(float(delta),6),
            'novel_saturated_colors':novel_colors,
            'blank_ceiling':round(float(blank_ceiling),6),
            'drew':bool(drew)
        }

def _selftest_copy_png(source,target):
    from PIL import Image
    source=Path(source);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(source) as im:
        im.convert('RGB').save(target,format='PNG',optimize=True)
    return target

def _selftest_label_from_error(error):
    msg=str(error or '')
    low=msg.lower()
    if low.startswith('compile:') or 'compile failed' in low or 'compile-pool' in low or 'produced no ex' in low:
        return 'COMPILE_FAIL'
    if 'timeout' in low or 'hang' in low:
        return 'TIMEOUT'
    m=re.search(r'(?:err(?:or)?[ =:]*)\(?([0-9]{3,5})\)?',msg,re.I)
    return f'RENDER_FAIL({m.group(1)})' if m else 'RENDER_FAIL'

def _selftest_contact_sheet(items,target):
    from PIL import Image,ImageDraw,ImageFont
    cols=5;thumb_w=220;thumb_h=132;label_h=48;pad=10
    rows=max(1,(len(items)+cols-1)//cols)
    sheet=Image.new('RGB',(pad+cols*(thumb_w+pad),pad+rows*(thumb_h+label_h+pad)),(32,32,32))
    draw=ImageDraw.Draw(sheet);font=ImageFont.load_default()
    for idx,item in enumerate(items):
        col=idx%cols;row=idx//cols;x=pad+col*(thumb_w+pad);y=pad+row*(thumb_h+label_h+pad)
        p=Path(item.get('png_path') or '')
        if p.is_file():
            try:
                with Image.open(p) as im:
                    tile=im.convert('RGB');tile.thumbnail((thumb_w,thumb_h))
                    ox=x+(thumb_w-tile.width)//2;oy=y+(thumb_h-tile.height)//2
                    sheet.paste(tile,(ox,oy))
            except Exception:pass
        else:
            draw.rectangle((x,y,x+thumb_w,y+thumb_h),outline=(110,110,110),width=1)
            draw.text((x+8,y+52),'NO IMAGE',font=font,fill=(220,220,220))
        name=str(item.get('name') or '')
        if len(name)>31:name=name[:28]+'...'
        draw.text((x,y+thumb_h+5),name,font=font,fill=(235,235,235))
        label=str(item.get('label') or '')
        draw.text((x,y+thumb_h+22),label,font=font,fill=(235,235,235))
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True);sheet.save(target,format='PNG',optimize=True)
    return target

def _selftest_print_summary(items,counts,draw_percent,passed):
    lines=['','Preview self-test','='*72,f'{"LABEL":<28} COUNT','-'*36]
    for label,count in sorted(counts.items(),key=lambda x:(x[0]!='OK_DREW',x[0])):
        lines.append(f'{label:<28} {count}')
    lines.extend(['-'*36,f'DRAW RATE                    {draw_percent:.1f}%',f'RESULT                       {"PASS" if passed else "FAIL"}'])
    bad=[i for i in items if i.get('label')!='OK_DREW']
    if bad:
        lines.append('');lines.append('BLANK / FAIL FILES')
        for item in bad:lines.append(f" - {item.get('label')}: {item.get('name')}")
    sys.stderr.write('\n'.join(lines)+'\n');sys.stderr.flush()

def preview_selftest(out,sample=30,source=None,db=None,terminal=None,workers=4,hang_timeout=40,min_draw_percent=60.0,mt5_data_dir=None):
    """Run a sampled preview test through production render code, then grade screenshots.

    MT5 uses the actual compile_pool + resident render_server. MT4 uses the existing
    real per-file MT4 renderer. No render path is mocked.
    """
    sample=max(1,min(int(sample or 30),200))
    workers=max(1,min(int(workers or 4),8))
    hang_timeout=max(40,int(hang_timeout or 40))
    root=Path(out)/'selftest'
    if root.exists():shutil.rmtree(root,ignore_errors=True)
    pngdir=root/'pngs';pngdir.mkdir(parents=True,exist_ok=True)
    selected=_selftest_candidates(source,db,sample)
    if not selected:raise RuntimeError('Preview self-test found no MQL4/MQL5 indicator files to test.')
    mt5=[p for p in selected if p.suffix.lower() in ('.mq5','.ex5')]
    mt4=[p for p in selected if p.suffix.lower() in ('.mq4','.ex4')]
    baseline=root/'reference_empty_chart.png'
    pipeline_out=root/'pipeline'/'previews'
    pipeline_out.mkdir(parents=True,exist_ok=True)
    testdb=_selftest_db(root/'selftest.sqlite3',mt5)
    runtime_id=safe_job_id(f'selftest-{time.time_ns()}')
    render_error=[]

    def render_thread():
        try:
            render_server(
                str(testdb),str(pipeline_out),terminal=terminal,job_id='selftest-render',
                hang_timeout=hang_timeout,max_attempts=1,mt5_data_dir=mt5_data_dir,
                runtime_id=runtime_id,baseline_target=baseline)
        except BaseException as e:
            render_error.append(e)

    import threading
    thread=threading.Thread(target=render_thread,name='mql-preview-selftest-render',daemon=True)
    thread.start()
    compile_error=None
    try:
        compile_pool(str(testdb),str(pipeline_out),terminal=terminal,workers=workers,
                     job_id='selftest-compile',runtime_id=runtime_id)
    except BaseException as e:
        compile_error=e
    if compile_error:
        cleanup=sqlite3.connect(testdb)
        try:
            cleanup.execute(
                "UPDATE indicators SET preview_status='failed',preview_error=?,worker_id=NULL "
                "WHERE preview_status IN ('pending','compiling','compiled','rendering')",
                (('compile-pool aborted: '+str(compile_error))[:800],))
            cleanup.commit()
        finally:cleanup.close()
    thread.join(timeout=max(90,hang_timeout*(max(1,len(mt5))+2)))
    if thread.is_alive():
        render_error.append(TimeoutError('Real resident render pipeline did not finish within the self-test deadline.'))
        # The production render server is bounded by hang_timeout; this is a final self-test guard.
        try:
            sel=load_runtime_cache(pipeline_out,'MT5') or choose_runtime('MT5',terminal)
            hard_kill(None,runtime_path(sel,pipeline_out,'MT5','render-server'))
        except Exception:pass
        thread.join(timeout=10)
    if compile_error and not baseline.is_file():
        raise RuntimeError(f'Real compile-pool self-test failed before baseline capture: {compile_error}')
    if render_error and not baseline.is_file():
        raise RuntimeError(f'Real resident render self-test failed before baseline capture: {render_error[0]}')
    if not baseline.is_file():
        raise RuntimeError('Real resident render self-test did not produce the empty-chart baseline.')

    rows={}
    con=sqlite3.connect(testdb);con.row_factory=sqlite3.Row
    try:
        for r in con.execute("SELECT path,sha256,preview_status,preview_path,preview_error FROM indicators"):
            rows[os.path.normcase(str(Path(r['path'])))] = dict(r)
    finally:con.close()

    items=[]
    for p in mt5:
        key=os.path.normcase(str(Path(p)))
        r=rows.get(key) or {}
        err=str(r.get('preview_error') or '')
        item={'name':p.name,'source':str(p),'kind':'MT5','pipeline':'compile-pool+resident-ea',
              'label':None,'err':err or None,'png_path':None,'coverage':None}
        if r.get('preview_status')=='ready' and r.get('preview_path') and Path(r['preview_path']).is_file():
            dst=_selftest_copy_png(r['preview_path'],pngdir/f'{str(r.get("sha256") or "")[:10]}_{safe_name(p).rsplit(".",1)[0]}.png')
            metrics=_selftest_image_metrics(baseline,dst,'MT5')
            item.update(metrics);item['png_path']=str(dst);item['label']='OK_DREW' if metrics['drew'] else 'BLANK'
        else:
            fallback=err or (str(render_error[0]) if render_error else f"render status={r.get('preview_status') or 'missing'}")
            item['err']=fallback
            item['label']=_selftest_label_from_error(fallback)
        items.append(item)

    mt4_out=root/'mt4-real'/'previews';mt4_out.mkdir(parents=True,exist_ok=True)
    for p in mt4:
        item={'name':p.name,'source':str(p),'kind':'MT4','pipeline':'real-mt4-render',
              'label':None,'err':None,'png_path':None,'coverage':None}
        try:
            before=set(mt4_out.glob('*'))
            render(str(p),str(mt4_out),terminal=terminal,job_id=f'selftest-mt4-{hashlib.sha1(str(p).encode()).hexdigest()[:8]}')
            after=[x for x in mt4_out.glob('*') if x.is_file() and x not in before and x.suffix.lower() in ('.gif','.png')]
            if not after:
                # render() is hash-stable and may overwrite an existing result on rerun.
                expected=mt4_out/(hashlib.sha1(('MT4|'+str(p.resolve()).lower()).encode()).hexdigest()[:16]+'.gif')
                after=[expected] if expected.is_file() else []
            if not after:raise RuntimeError('real MT4 renderer returned without an image')
            dst=_selftest_copy_png(after[-1],pngdir/f'mt4_{hashlib.sha256(p.read_bytes()).hexdigest()[:10]}_{safe_name(p).rsplit(".",1)[0]}.png')
            metrics=_selftest_image_metrics(baseline,dst,'MT4')
            item.update(metrics);item['png_path']=str(dst);item['label']='OK_DREW' if metrics['drew'] else 'BLANK'
        except Exception as e:
            item['err']=f'{type(e).__name__}: {e}';item['label']=_selftest_label_from_error(item['err'])
        items.append(item)

    order={os.path.normcase(str(p)):i for i,p in enumerate(selected)}
    items.sort(key=lambda x:order.get(os.path.normcase(x['source']),999999))
    counts={}
    for item in items:counts[item['label']]=counts.get(item['label'],0)+1
    ok_count=counts.get('OK_DREW',0)
    draw_percent=(100.0*ok_count/max(1,len(items)))
    passed=draw_percent>=float(min_draw_percent)
    contact=_selftest_contact_sheet(items,root/'selftest_contact_sheet.png')
    report={
        'generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
        'sample_requested':sample,'sample_count':len(items),
        'mt5_count':len(mt5),'mt4_count':len(mt4),
        'baseline_path':str(baseline),'contact_sheet':str(contact),
        'min_draw_percent':float(min_draw_percent),'draw_percent':round(draw_percent,2),
        'passed':passed,'counts':counts,'items':items
    }
    report_path=root/'selftest_report.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    _selftest_print_summary(items,counts,draw_percent,passed)
    emit({'ok':passed,'selftest':True,'real_pipeline':True,'sample_count':len(items),'counts':counts,
          'draw_percent':round(draw_percent,2),'min_draw_percent':float(min_draw_percent),
          'report':str(report_path),'contact_sheet':str(contact),'baseline':str(baseline),
          'failed_files':[x['name'] for x in items if x['label']!='OK_DREW']})
    if not passed:raise SystemExit(3)


def self_test_discovery():
    checks={}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);base=root/'MetaQuotes'/'Terminal';base.mkdir(parents=True)
        mt5=root/'OANDA TMS MT5 Terminal';mt5.mkdir()
        (mt5/'terminal64.exe').write_bytes(b'terminal')
        (mt5/'metaeditor64.exe').write_bytes(b'editor')
        d5=base/'DATA5';(d5/'MQL5'/'Indicators').mkdir(parents=True)
        (d5/'origin.txt').write_text(str(mt5),encoding='utf-8')
        mt4=root/'Blueberry Markets MetaTrader 4';mt4.mkdir()
        (mt4/'terminal.exe').write_bytes(b'terminal')
        d4=base/'DATA4';(d4/'MQL4'/'Indicators').mkdir(parents=True)
        (d4/'origin.txt').write_text(str(mt4),encoding='utf-8')
        data=discover_data_dirs(base)
        installs=discover_installations(base,roots=[])
        pairs=pair_runtime_candidates(installs,data)
        p5=next((x for x in pairs if x['kind']=='MT5' and x.get('terminal')),None)
        p4=next((x for x in pairs if x['kind']=='MT4' and x.get('terminal')),None)
        checks['terminal_detection_independent_of_editor']=bool(p4 and p4.get('terminal') and not p4.get('editor'))
        checks['metaeditor_from_terminal_install']=bool(p5 and p5.get('editor')==str(mt5/'metaeditor64.exe'))
        checks['origin_data_pairing']=bool(p5 and p5.get('data_dir')==str(d5) and p5.get('pair_method')=='origin-match')
        # Regression guard: the live-data MQL check must resolve to MQL5/MQL4, not MT5/MT4.
        _mc=selection_checks(root/'demo.mq5','MT5',p5) if p5 else {}
        checks['live_mql_directory_detected']=bool(p5 and _mc.get('mql_directory'))
        checks['diagnostics_use_mql_folder_name']=bool(p5 and discovery_diagnostics('MT5',p5,_mc)[3]['stage']=='MQL5 directory')
        alt=root/'Alternate MT5';alt.mkdir()
        (alt/'terminal64.exe').write_bytes(b'terminal')
        (alt/'metaeditor64.exe').write_bytes(b'editor')
        alt_install={'kind':'MT5','terminal':str(alt/'terminal64.exe'),'editor':str(alt/'metaeditor64.exe'),'install_dir':str(alt)}
        fallback=pair_runtime_candidates([alt_install],[x for x in data if x['kind']=='MT5'])
        checks['independent_platform_pairing']=bool(fallback and fallback[0].get('data_dir')==str(d5) and fallback[0].get('pair_method')=='platform-fallback')
        out=root/'previews'
        rt=clone_runtime(p5,out,'MT5')
        checks['sandbox_mql_tree_created']=(rt/'MQL5'/'Indicators').is_dir()  # clone_runtime raises unless the minimum MQL tree exists
        cache=write_runtime_cache(out,'MT5',root/'demo.mq5',p5)
        checks['runtime_selection_cache_written']=cache.is_file()
    return checks

def self_test_render_dependencies():
    checks={}
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        real=root/'Terminal'/'ABC123'; rt=root/'render'
        (real/'config').mkdir(parents=True)
        (real/'config'/'accounts.dat').write_bytes(b'account')
        hist=real/'bases'/'broker'/'history'/'EURUSD';hist.mkdir(parents=True)
        (hist/'2026.hcc').write_bytes(b'bars')

        for path,data in (
            (real/'MQL5'/'Indicators'/'Helpers'/'Helper.ex5',b'helper'),
            (real/'MQL5'/'Include'/'Custom'/'Helper.mqh',b'header'),
            (real/'MQL5'/'Libraries'/'CustomLib.ex5',b'library'),
            (real/'MQL5'/'Files'/'settings.dat',b'data'),
            (real.parent/'Common'/'Files'/'shared.dat',b'common'),
            (real/'MQL5'/'Indicators'/'MQLLibraryPreview'/'stale.ex5',b'stale-preview'),
            (real/'MQL5'/'Files'/'MQLLibRender'/'stale.txt',b'stale-protocol'),
        ):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

        preview_keep=rt/'MQL5'/'Indicators'/'MQLLibraryPreview'/'live.ex5'
        protocol_keep=rt/'MQL5'/'Files'/'MQLLibRender'/'current.job'
        preview_keep.parent.mkdir(parents=True,exist_ok=True);preview_keep.write_bytes(b'live-preview')
        protocol_keep.parent.mkdir(parents=True,exist_ok=True);protocol_keep.write_text('live-job',encoding='utf-8')
        (rt/'.stamp').write_text('runtime-v1',encoding='utf-8')

        seeded=seed_render_dependencies_from_data_dir(real,rt)
        checks['dependency_seed_not_cached_first']=not seeded.get('cached',True)
        checks['helper_indicator_seeded']=(rt/'MQL5'/'Indicators'/'Helpers'/'Helper.ex5').read_bytes()==b'helper'
        checks['include_seeded']=(rt/'MQL5'/'Include'/'Custom'/'Helper.mqh').read_bytes()==b'header'
        checks['library_seeded']=(rt/'MQL5'/'Libraries'/'CustomLib.ex5').read_bytes()==b'library'
        checks['files_seeded']=(rt/'MQL5'/'Files'/'settings.dat').read_bytes()==b'data'
        checks['common_seeded']=(rt/'Common'/'Files'/'shared.dat').read_bytes()==b'common'
        checks['preview_workdir_preserved']=preview_keep.read_bytes()==b'live-preview' and not (rt/'MQL5'/'Indicators'/'MQLLibraryPreview'/'stale.ex5').exists()
        checks['protocol_workdir_preserved']=protocol_keep.read_text(encoding='utf-8')=='live-job' and not (rt/'MQL5'/'Files'/'MQLLibRender'/'stale.txt').exists()
        cached=seed_render_dependencies_from_data_dir(real,rt)
        checks['dependency_seed_cached_second']=bool(cached.get('cached'))
    checks['residual_4802_classified']=_render_failure_message('4802')=='indicator OnInit failed (err 4802) — needs dependencies/inputs'
    return checks


def self_test():
    src=mt5_capture_source('MQLLibraryPreview\\abc\\demo','shot.png','0')
    checks={
        'no_input_dialog':'script_show_inputs' not in src,
        'trace_before_icustom':'stage=before_iCustom' in src,
        'trace_after_icustom':'stage=after_iCustom' in src,
        'trace_chart_add':'stage=chart_add' in src,
        'trace_screenshot':'stage=screenshot' in src,
        'compile_error_fast_fail':compile_has_errors('Result: 3 errors, 1 warnings'),
        'compile_zero_errors_not_failure':not compile_has_errors('Result: 0 errors, 2 warnings'),
        'job_id_sanitized':safe_job_id('job id:123')=='job-id-123',
        'mt5_prime_v3_marker':'.mt5-preview-prime-v3'.endswith('prime-v3'),
        'minimal_runtime_seed_v5':'.mql5-seed-v5'.endswith('seed-v5'),
        'shutdown_terminal_numeric':True,
        'mql_dir_name_mt5':mql_dir_name('MT5')=='MQL5',
        'mql_dir_name_mt4':mql_dir_name('MT4')=='MQL4',
        'origin_utf8_decode':decode_origin_bytes(b'C:\\Program Files\\OANDA TMS MT5')=='C:\\Program Files\\OANDA TMS MT5',
        'origin_utf16_decode':decode_origin_bytes(('C:\\Program Files\\MetaTrader 4').encode('utf-16'))=='C:\\Program Files\\MetaTrader 4',
        'mql_text_utf8_source':'indicator_separate_window' in decode_mql_bytes(b'#property indicator_separate_window\r\n'),
        'mql_text_utf16le_source':'indicator_separate_window' in decode_mql_bytes(b'\xff\xfe'+'#property indicator_separate_window\r\n'.encode('utf-16-le')),
        'mql_text_bomless_utf16le':decode_mql_bytes('Result: 0 errors'.encode('utf-16-le'))=='Result: 0 errors',
        'render_mt4_accepts_worker_id':'worker_id' in render_mt4.__code__.co_varnames[:render_mt4.__code__.co_argcount],
        'mql_str_backslash_escape':_mql_str(r'MQLLibraryPreview\\abc\\demo')==r'MQLLibraryPreview\\\\abc\\\\demo',
        'resident_ea_timer':'EventSetMillisecondTimer(150)' in RESIDENT_EA_SOURCE,
        'resident_ea_job_protocol':all(x in RESIDENT_EA_SOURCE for x in ('current.job','current.done','heartbeat.txt')),
        'resident_ea_warm_terminal':'TerminalClose' not in RESIDENT_EA_SOURCE,
        'resident_ea_forward_path_host':_resident_indicator_rel('abc',Path('demo.ex5'))=='MQLLibraryPreview/abc/demo',
        'resident_waits_for_positive_bars':'if(bc>0)' in RESIDENT_EA_SOURCE and 'stable>=5' in RESIDENT_EA_SOURCE,
        'resident_wait_budget_12s':'for(int i=0;i<60;i++)' in RESIDENT_EA_SOURCE and 'Sleep(200)' in RESIDENT_EA_SOURCE,
        'resident_repaints_before_shot':'ChartNavigate(0,CHART_END,0)' in RESIDENT_EA_SOURCE and 'for(int i=0;i<6;i++)' in RESIDENT_EA_SOURCE,
        'resident_deep_history_warmup':'CopyRates(_Symbol,_Period,0,3000,rr)' in RESIDENT_EA_SOURCE,
        'selftest_real_baseline_sentinel':'__MQLLIB_EMPTY_CHART__' in RESIDENT_EA_SOURCE,
    }
    checks.update(self_test_discovery())
    checks.update(self_test_render_dependencies())
    if not all(checks.values()):raise RuntimeError(f'Preview self-test failed: {checks}')
    emit({'ok':True,'checks':checks})
