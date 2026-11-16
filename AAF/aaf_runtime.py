# @noindex
"""Shared interpreter discovery for the REAPER actions."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def external_python(folder):
    config_path = folder / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    configured = config.get("python", "").strip()
    candidates = [configured] if configured else []
    if not configured:
        candidates += [str(folder / ".venv" / "bin" / "python"),
                       str(folder / ".venv" / "Scripts" / "python.exe")]
        if "python" in Path(sys.executable).name.lower():
            candidates.append(sys.executable)
        framework = Path("/Library/Frameworks/Python.framework/Versions")
        if framework.is_dir():
            candidates += [str(p / "bin" / "python3") for p in sorted(framework.glob("3.*"),
                           key=lambda p: tuple(int(n) for n in p.name.split(".") if n.isdigit()), reverse=True)]
        candidates += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", shutil.which("python3"), shutil.which("python")]
    failures, seen = [], set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if not Path(candidate).is_file():
            continue
        try:
            run = subprocess.run([candidate, str(folder / "aaf_ui.py"), "--probe"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if run.returncode == 0:
                return candidate
            failures.append(candidate + ": " + run.stderr.decode("utf-8", errors="replace")[-700:])
        except Exception as exc:
            failures.append(candidate + ": " + str(exc))
    raise RuntimeError("No external Python 3.9+ with Tkinter found.\n" +
                       "Install Python from python.org, then set its full executable path in config.json.\n\n" + "\n".join(failures))
