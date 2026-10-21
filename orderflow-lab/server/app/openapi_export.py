"""Write the OpenAPI contract generated from the app.

    python3 -m app.openapi_export            # writes server/openapi.json
    python3 -m app.openapi_export --check    # exit 1 when openapi.json is out of date
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .main import create_app

TARGET = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    spec = create_app(Settings(), connect=False).openapi()
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("openapi.json is out of date: run python3 -m app.openapi_export", file=sys.stderr)
            return 1
        print("openapi.json is up to date")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
