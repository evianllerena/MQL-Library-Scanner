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


if __name__ == '__main__':
    unittest.main()
