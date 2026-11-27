"""Offline, narrowly-scoped repair for the known api.py startup corruption.

No network access is used. Only the exact stray JavaScript fragment reported
by the startup diagnostic is removed; unknown syntax errors are left untouched.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import sys


# The known accidental JavaScript tail was appended to the uvicorn.run line.
KNOWN_JS = re.compile(
    r"(?m)^(?P<python>\s*uvicorn\.run\([^\n]*\))\+?\s*\r?\n"
    r"(?P<javascript>\s*mt\(pnl\);score\.style\.color=positive\?"
    r"'var\(--gn\)':'var\(--rd\)';\}\s*)$"
)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    target = root / "api.py"
    if not target.is_file():
        print("[AUTO-REPAIR] api.py was not found; nothing changed.")
        return 1

    original = target.read_bytes()
    try:
        source = original.decode("utf-8-sig")
        compile(source, str(target), "exec")
        print("[AUTO-REPAIR] api.py syntax is already valid; no repair needed.")
        return 0
    except SyntaxError as exc:
        print(f"[AUTO-REPAIR] Syntax error at line {exc.lineno}: {exc.msg}")

    repaired, count = KNOWN_JS.subn(r"\g<python>", source, count=1)
    if count != 1:
        print("[AUTO-REPAIR] This is not the known appended-JavaScript error.")
        print("[AUTO-REPAIR] No files were changed; inspect the reported syntax error.")
        return 1

    try:
        compile(repaired, str(target), "exec")
    except SyntaxError as exc:
        print(f"[AUTO-REPAIR] Candidate repair still has a syntax error at line {exc.lineno}: {exc.msg}")
        print("[AUTO-REPAIR] Original api.py was not changed.")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"api.py.backup-{stamp}")
    try:
        backup.write_bytes(original)
        target.write_text(repaired, encoding="utf-8", newline="")
    except OSError as exc:
        print(f"[AUTO-REPAIR] Could not safely write repaired file: {exc}")
        if backup.exists():
            target.write_bytes(original)
        return 1

    print(f"[AUTO-REPAIR] Removed the recognized stray JavaScript fragment.")
    print(f"[AUTO-REPAIR] Original file backed up as {backup.name}")
    print("[AUTO-REPAIR] Repaired api.py passed Python syntax validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
