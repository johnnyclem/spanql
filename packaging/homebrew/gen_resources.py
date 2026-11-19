#!/usr/bin/env python3
"""Print Homebrew `resource` blocks for spanql's runtime dependencies, from uv.lock.

    uv export --no-dev --extra code --extra pdf --no-hashes --no-emit-project \
        | python3 packaging/homebrew/gen_resources.py

Reads package names/versions from stdin (requirements format) and the sdist URL + sha256 for
each from uv.lock, so the formula pins exactly what CI tested. (`brew update-python-resources`
does the same job from PyPI if you prefer.)
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

LOCK = Path(__file__).resolve().parents[2] / "uv.lock"


def main() -> int:
    lock = tomllib.loads(LOCK.read_text())
    by_name = {(p["name"], p["version"]): p for p in lock["package"]}
    for line in sys.stdin:
        line = line.split(";")[0].strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)==([^\s]+)", line)
        if not m:
            continue
        name, version = m.group(1).lower().replace("_", "-"), m.group(2)
        pkg = by_name.get((name, version))
        if pkg is None or "sdist" not in pkg:
            print(f"# {name}=={version}: no sdist in uv.lock", file=sys.stderr)
            continue
        sd = pkg["sdist"]
        sha = sd["hash"].removeprefix("sha256:")
        print(f'  resource "{name}" do\n    url "{sd["url"]}"\n    sha256 "{sha}"\n  end\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
