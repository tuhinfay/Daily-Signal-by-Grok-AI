"""Offline repair for the known duplicated JavaScript tail in api.py.

This helper never contacts GitHub or any remote server. It only removes the
recognized JavaScript fragment accidentally appended after the Python entry
point, after validating the repaired source and backing up the original.
"""
from __future__ import annotations

import datetime
import pathlib
import re


ENTRY = 'if __name__ == "__main__":'
KNOWN_CONTAMINATION = (
    "+fmt(pnl)",
    "+mt(pnl)",
    "score.style.color=positive?",
    "card.querySelectorAll('.vals > div')",
)


def main() -> int:
    target = pathlib.Path(__file__).resolve().parent.parent / "api.py"
    if not target.is_file():
        print("[AUTO-REPAIR] api.py was not found; nothing changed.")
        return 1

    original = target.read_bytes()
    try:
        source = original.decode("utf-8-sig")
        compile(source, str(target), "exec")
        print("[AUTO-REPAIR] api.py syntax is valid; no repair needed.")
        return 0
    except SyntaxError as exc:
        print(f"[AUTO-REPAIR] Syntax error at line {exc.lineno}: {exc.msg}")

    marker_at = source.find(ENTRY)
    if marker_at < 0:
        print("[AUTO-REPAIR] Python entry point not found; unknown error, no changes made.")
        return 1

    tail = source[marker_at:]
    if not any(token in tail for token in KNOWN_CONTAMINATION):
        print("[AUTO-REPAIR] Error does not match the known appended-JavaScript corruption.")
        print("[AUTO-REPAIR] Original api.py was preserved.")
        return 1

    repaired = (
        source[:marker_at]
        + ENTRY
        + "\n    port = int(os.environ.get(\"PORT\", 8000))"
        + "\n    uvicorn.run(\"api:app\", host=\"0.0.0.0\", port=port, reload=False)\n"
    )
    try:
        compile(repaired, str(target), "exec")
    except SyntaxError as exc:
        print(f"[AUTO-REPAIR] Repaired candidate still fails at line {exc.lineno}: {exc.msg}")
        print("[AUTO-REPAIR] Original api.py was preserved.")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"api.py.backup-{stamp}")
    try:
        backup.write_bytes(original)
        target.write_text(repaired, encoding="utf-8")
    except OSError as exc:
        print(f"[AUTO-REPAIR] Could not safely install repair: {exc}")
        if backup.exists():
            target.write_bytes(original)
        return 1

    print("[AUTO-REPAIR] Removed the recognized JavaScript tail; syntax check passed.")
    print(f"[AUTO-REPAIR] Original saved as {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
