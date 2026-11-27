"""Restore api.py from the project's canonical GitHub branch if local syntax is broken.

This is invoked by start.bat only after the local Python syntax check fails.
The existing file is backed up before replacement.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import sys
import tempfile
import urllib.request

REPOSITORY_URL = (
    "https://raw.githubusercontent.com/tuhinfay/"
    "Daily-Signal-by-Grok-AI/fix/trades-live-stability-auth-dca/api.py"
)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    target = root / "api.py"
    print("[AUTO-REPAIR] Local api.py has a syntax error.")
    print("[AUTO-REPAIR] Downloading the verified project copy from GitHub...")

    try:
        request = urllib.request.Request(
            REPOSITORY_URL,
            headers={"User-Agent": "Daily-Signal-Startup-Repair"},
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            source_bytes = response.read()
        source = source_bytes.decode("utf-8-sig")
        compile(source, str(target), "exec")
    except Exception as exc:
        print(f"[AUTO-REPAIR] Could not retrieve/validate the repair file: {exc}")
        print("[AUTO-REPAIR] Check your internet connection; original api.py was not changed.")
        return 1

    if target.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"api.py.backup-{stamp}")
        try:
            backup.write_bytes(target.read_bytes())
            print(f"[AUTO-REPAIR] Original file backed up to: {backup.name}")
        except OSError as exc:
            print(f"[AUTO-REPAIR] Could not back up original api.py: {exc}")
            return 1

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".py", prefix="api-repair-", dir=root, delete=False
        ) as temp:
            temp.write(source_bytes)
            temp_name = temp.name
        os.replace(temp_name, target)
        compile(target.read_text(encoding="utf-8-sig"), str(target), "exec")
    except Exception as exc:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
        print(f"[AUTO-REPAIR] Could not install repaired api.py: {exc}")
        return 1

    print("[AUTO-REPAIR] api.py restored and Python syntax validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
