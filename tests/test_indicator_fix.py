"""Regression tests for tools/indicator_fix (P0.6). Pure Python: no MetaTrader, no claude CLI, no F: drive."""
import ast
import importlib
import os
import re
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / 'tools' / 'indicator_fix'
sys.path.insert(0, str(TOOLS))


class MachinePathsTest(unittest.TestCase):
    # fix_paths.py holds the defaults; mqlc.py keeps its own env-or-default lookups because a copy of it
    # runs from <BUILD>\tools without fix_paths.py beside it.
    ALLOWED = {'fix_paths.py', 'mqlc.py'}
    MACHINE = re.compile(r'^[A-Za-z]:\\|^/[a-z]/|\\Users\\Evision')

    def test_no_hardcoded_machine_paths_outside_fix_paths(self):
        hits = []
        for p in sorted(TOOLS.glob('*.py')):
            if p.name in self.ALLOWED:
                continue
            tree = ast.parse(p.read_text(encoding='utf-8'))
            doc = tree.body[0].value if tree.body and isinstance(tree.body[0], ast.Expr) else None
            for node in ast.walk(tree):
                if node is doc or not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if self.MACHINE.search(node.value):
                    hits.append(f'{p.name}:{node.lineno}: {node.value[:60]}')
        self.assertEqual(hits, [])

    def test_defaults_unchanged_and_env_overrides(self):
        import fix_paths
        saved = {k: os.environ.pop(k, None) for k in ('MQLFIX_BUILD', 'MQLFIX_RT', 'MQLFIX_MQLC')}
        try:
            importlib.reload(fix_paths)
            self.assertEqual(str(fix_paths.BUILD), str(Path(r'F:\MQLFIX_BUILD')))
            self.assertEqual(str(fix_paths.rt(3)), str(Path(r'F:\MQLFIX_RT') / 'rt3'))
            self.assertEqual(fix_paths.MQLC, str(Path(r'F:\MQLFIX_BUILD') / 'tools' / 'mqlc.py'))
            os.environ['MQLFIX_BUILD'] = '/tmp/build'
            os.environ['MQLFIX_RT'] = '/tmp/rt'
            importlib.reload(fix_paths)
            self.assertEqual(fix_paths.BUILD, Path('/tmp/build'))
            self.assertEqual(fix_paths.rt(2), Path('/tmp/rt/rt2'))
            self.assertEqual(fix_paths.MQLC, str(Path('/tmp/build/tools/mqlc.py')))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            importlib.reload(fix_paths)


class PrePy312SyntaxTest(unittest.TestCase):
    """Before Python 3.12 (PEP 701) a replacement field may not reuse the f-string's own quote character."""

    def test_no_f_string_reuses_its_own_quote(self):
        import io
        import tokenize
        if sys.version_info < (3, 12):          # older interpreters reject such files outright
            for p in sorted(TOOLS.glob('*.py')):
                compile(p.read_text(encoding='utf-8'), str(p), 'exec')
            return
        hits = []
        for p in sorted(TOOLS.glob('*.py')):
            quotes = []                          # quote char of each enclosing f-string
            for tok in tokenize.generate_tokens(io.StringIO(p.read_text(encoding='utf-8')).readline):
                if tok.type == tokenize.FSTRING_START:
                    q = tok.string.lstrip('rRbBfFuU')[:1]
                    if q in quotes:
                        hits.append(f'{p.name}:{tok.start[0]}')
                    quotes.append(q)
                elif tok.type == tokenize.FSTRING_END:
                    quotes.pop()
                elif tok.type == tokenize.STRING and quotes:
                    if tok.string.lstrip('rRbBfFuU')[:1] in quotes:
                        hits.append(f'{p.name}:{tok.start[0]}')
        self.assertEqual(hits, [])


class RepairPoolTest(unittest.TestCase):
    """repair_ai.run_job must hand its MT5 runtime token back to RT_POOL however the job ends."""

    def setUp(self):
        import subprocess
        import tempfile
        from unittest import mock
        import repair_ai
        self.ra, self.mock, self.subprocess = repair_ai, mock, subprocess
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        src = tmp / 'src.mq5'
        src.write_text('int OnInit(){return 0;}\n', encoding='utf-8')
        self.job = {'name': 'demo', 'source': str(src), 'problem': 'runtime'}
        self.patches = [mock.patch.object(repair_ai, 'R', tmp / 'R'),
                        mock.patch.object(repair_ai, 'OUT', tmp / 'out.jsonl')]
        for p in self.patches:
            p.start()
        self.size = repair_ai.RT_POOL.qsize()
        repair_ai.STOP.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.ra.STOP.clear()
        self.tmp.cleanup()

    def run_with(self, fake_run):
        with self.mock.patch.object(self.ra.subprocess, 'run', side_effect=fake_run):
            return self.ra.run_job(self.job)

    def test_token_returned_when_claude_cannot_start(self):
        def fake_run(cmd, **kw):
            raise FileNotFoundError(cmd[0])
        with self.assertRaises(FileNotFoundError):
            self.run_with(fake_run)
        self.assertEqual(self.ra.RT_POOL.qsize(), self.size)

    def test_token_returned_when_verification_cannot_start(self):
        def fake_run(cmd, **kw):
            if cmd[0] == self.ra.CLAUDE:
                return self.subprocess.CompletedProcess(cmd, 0, stdout='DONE\n')
            raise PermissionError(cmd[0])
        with self.assertRaises(PermissionError):
            self.run_with(fake_run)
        self.assertEqual(self.ra.RT_POOL.qsize(), self.size)

    def test_token_returned_exactly_once_on_usage_limit_and_success(self):
        def limit(cmd, **kw):
            return self.subprocess.CompletedProcess(cmd, 0, stdout="You've hit your usage limit\n")
        self.assertTrue(self.run_with(limit)['skipped'])
        self.assertEqual(self.ra.RT_POOL.qsize(), self.size)
        self.ra.STOP.clear()

        def ok(cmd, **kw):
            return self.subprocess.CompletedProcess(cmd, 0, stdout='DONE\nPASS\n')
        self.assertTrue(self.run_with(ok)['passes'])
        self.assertEqual(self.ra.RT_POOL.qsize(), self.size)


class DriversRunningTest(unittest.TestCase):
    """orchestrate.qa() waits while drivers_running(); the query must not count itself."""

    def test_only_python_running_qa_driver_counts(self):
        import orchestrate
        ps = ("powershell -NoProfile -Command \"Get-CimInstance Win32_Process | Where-Object "
              "{ $_.CommandLine -match 'qa_driver.py' }\"")
        self.assertFalse(orchestrate.is_qa_driver('powershell.exe', ps))
        self.assertFalse(orchestrate.is_qa_driver('bash.exe', 'bash -c "python qa_driver.py A"'))
        self.assertFalse(orchestrate.is_qa_driver('python.exe', r'python F:\x\orchestrate.py'))
        self.assertFalse(orchestrate.is_qa_driver('python.exe', None))
        self.assertTrue(orchestrate.is_qa_driver(
            'python.exe', r'"C:\Py312\python.exe" C:\tools\qa_driver.py A --rt F:\MQLFIX_RT\rt1 --shard 0/4'))
        self.assertTrue(orchestrate.is_qa_driver('py.exe', 'py qa_driver.py B'))

    @unittest.skipUnless(sys.platform == 'win32', 'queries live Windows processes')
    def test_live_non_driver_mentioning_qa_driver_is_not_counted(self):
        # e.g. an editor with qa_driver.py open. With the old query this process plus the query's own
        # PowerShell made 2 matches -> drivers_running() stayed True and qa() never started.
        # (A lone self-match did not show: PowerShell 5.1 gives no .Count for a single CimInstance.)
        import orchestrate
        import subprocess
        listing = subprocess.run(['powershell', '-NoProfile', '-Command',
                                  "@(Get-CimInstance Win32_Process -Filter \"Name LIKE 'py%'\" | "
                                  "Where-Object { $_.CommandLine -match 'qa_driver\\.py' }).Count"],
                                 capture_output=True, text=True).stdout.strip()
        if listing not in ('', '0'):
            self.skipTest('a python process mentioning qa_driver.py is running on this machine')
        bystander = subprocess.Popen('cmd /c "ping -n 30 127.0.0.1 >nul & rem qa_driver.py"',
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertFalse(orchestrate.drivers_running())
            driver = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'qa_driver.py'])
            try:
                self.assertTrue(orchestrate.drivers_running())     # a real python driver is still seen
            finally:
                driver.kill(); driver.wait()
        finally:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(bystander.pid)], capture_output=True)


if __name__ == '__main__':
    unittest.main()
