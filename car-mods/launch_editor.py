#!/usr/bin/env python3
"""Open the MM3 car workshop (editor.py) with the repository's build venv.

Windows: editor.cmd runs this file; pythonw is preferred so no console stays open.
Other systems: editor.sh runs this file. Arguments pass through to editor.py,
for example: launch_editor.py --car-id 9004 --tab model
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = (HERE / ".." / ".." / "..").resolve()  # car-mods -> car-tools -> tools -> mm3-recomp
GAME_DIR = REPO / "game_files"


def venv_python() -> str:
    """The repo's venv interpreter, preferring the windowless one on Windows.

    Started from a terminal, keep python.exe so --help and errors print there.
    """
    attached = sys.stdout is not None
    if sys.platform == "win32":
        names = ["python.exe", "pythonw.exe"] if attached else ["pythonw.exe", "python.exe"]
        candidates = [REPO / "tools" / "venv" / "Scripts" / name for name in names]
    else:
        candidates = [REPO / "tools" / "venv" / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    current = Path(sys.executable)
    if sys.platform == "win32" and not attached and current.name.lower() == "python.exe":
        windowless = current.with_name("pythonw.exe")
        if windowless.is_file():
            return str(windowless)
    return sys.executable


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # Defaults come first so anything the caller passes overrides them (argparse keeps the last value).
    command = [venv_python(), str(HERE / "editor.py"), "--game-dir", str(GAME_DIR), *args]
    return subprocess.call(command, cwd=str(HERE))


if __name__ == "__main__":
    raise SystemExit(main())
