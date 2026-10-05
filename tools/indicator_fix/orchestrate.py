r"""Unattended orchestration of the fix pipeline after the variants are built.
1. wait for the A and B compiles (A_compile.json / B_compile.json);
2. recompile every .mq5 that produced no .ex5, now with the include path (/inc), copying new .ex5
   files back beside their sources;
3. runtime QA: 4 parallel shards over A until nothing is left, then the same over B (B skips names
   already aligned in A). Progress -> F:\MQLFIX_BUILD\orchestrate.log"""
import shutil, subprocess, sys, time
from pathlib import Path

W = Path(r'F:\MQLFIX_BUILD')
HERE = Path(__file__).resolve().parent
LOG = W / 'orchestrate.log'

def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + msg + '\n')

def recompile_failed(v):
    failed = [p for p in (W / v).glob('c*/*.mq5') if not p.with_suffix('.ex5').exists()]
    rv = W / f'{v}R'
    shutil.rmtree(rv, ignore_errors=True)
    for i, p in enumerate(failed):
        d = rv / f'c{i // 500:03d}'; d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d / p.name)
    log(f'{v}: {len(failed)} without .ex5 -> recompiling with /inc')
    if not failed:
        return
    subprocess.run([sys.executable, str(HERE / 'compile_variant.py'), f'{v}R'], cwd=HERE)
    back = 0
    origin = {p.name: p for p in failed}
    for ex in rv.glob('c*/*.ex5'):
        src = origin.get(ex.with_suffix('.mq5').name)
        if src and ex.stat().st_size > 0:
            shutil.copy2(ex, src.with_suffix('.ex5')); back += 1
    log(f'{v}: recompile recovered {back} of {len(failed)}')

def drivers_running():
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'qa_driver.py' }).Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0) > 0

def qa(v):
    while drivers_running():          # never two drivers on one runtime copy
        time.sleep(30)
    procs = [subprocess.Popen([sys.executable, str(HERE / 'qa_driver.py'), v, '--rt', rf'F:\MQLFIX_RT\rt{k + 1}',
                               '--shard', f'{k}/4'], cwd=HERE,
                              stdout=open(W / f'qa_{v}_s{k}.progress.txt', 'a'), stderr=subprocess.STDOUT)
             for k in range(4)]
    for p in procs: p.wait()          # each driver exits when its shard has nothing left
    log(f'QA {v}: shard drivers finished')

if __name__ == '__main__':
    log('orchestrator start')
    while not ((W / 'A_compile.json').exists() and (W / 'B_compile.json').exists()):
        time.sleep(60)
    log('A and B compiles finished')
    for v in ('A', 'B'):
        recompile_failed(v)
    qa('A')
    qa('B')
    log('orchestrator done')
