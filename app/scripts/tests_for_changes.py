#!/usr/bin/env python3
"""Which test files are for what's changed since the last commit (committed or not, new files too)?

    python scripts/tests_for_changes.py        -> one test file per line (nothing if nothing matches)

A changed test file is itself. A changed module (sleepradiopi/.../conditions.py) is every test that imports a name
`conditions`, and test_conditions.py by name. The desktop and phone pages are the tests that read them. Only a guide
for quick runs while working: the full run (scripts/test.sh) is what a release is gated on.
"""
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
TESTS = APP / "tests"


def changed() -> list[str]:
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=APP, capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", str(APP)], cwd=root, capture_output=True, text=True, check=True).stdout
    names = []
    for line in out.splitlines():
        name = line[3:].split(" -> ")[-1].strip('"')
        names.append(str((Path(root) / name).relative_to(APP)))
    return names


def imports(text: str) -> str:
    """Just the import lines of a test file (indented ones too: many import inside the test)."""
    return "\n".join(l for l in text.splitlines() if re.match(r"\s*(from|import)\s", l))


def tests_for(names: list[str]) -> list[str]:
    tests = sorted(TESTS.glob("test_*.py"))
    texts = {t: t.read_text(errors="replace") for t in tests}
    found: set[Path] = set()
    for name in names:
        p = Path(name)
        if p.parts[:1] == ("tests",) and p.name.startswith("test_") and (APP / p).exists():
            found.add(APP / p)
        elif p.suffix == ".py" and p.parts[:1] == ("sleepradiopi",) and p.stem != "__init__":
            for t in tests:
                if t.stem == f"test_{p.stem}" or re.search(rf"\b{re.escape(p.stem)}\b", imports(texts[t])):
                    found.add(t)
        elif p.suffix == ".html":
            for t in tests:
                if p.name in texts[t]:
                    found.add(t)
    return sorted(str(t.relative_to(APP)) for t in found)


if __name__ == "__main__":
    for t in tests_for(changed()):
        print(t)
    sys.exit(0)
