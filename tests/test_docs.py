"""Docs must not tell people to run scripts that are not in the repo (P0.7)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ['README.md', 'BUILD_WINDOWS.md', 'REPOSITORY_SETUP.md', 'FIRST_TEST_PLAN.md']


class DocScriptRefsTest(unittest.TestCase):
    def test_referenced_powershell_scripts_exist(self):
        missing = []
        for name in DOCS:
            doc = ROOT / name
            if not doc.exists():
                continue
            for line in doc.read_text(encoding='utf-8', errors='replace').splitlines():
                if not re.search(r'(-File\s+|^\s*)\.[\\/]', line):    # an instruction to run it, not a mention
                    continue
                for ref in re.findall(r'\.[\\/]([\w\\/.-]+\.ps1)', line):
                    if not (ROOT / ref.replace('\\', '/')).exists():
                        missing.append(f'{name}: {ref}')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
