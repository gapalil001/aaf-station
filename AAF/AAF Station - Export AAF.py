# @noindex
# ReaScript Name: AAF Station - Export AAF
# Version: 0.2.2
"""Read the active project and open AAF Station's export window."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import reaper_python as reaper


def package_directory():
    compiled = _aaf_export.__code__.co_filename
    script = Path(globals().get("__file__") or compiled)
    if script.is_absolute() and (script.parent / "aaf_export_ui.py").is_file():
        return script.parent

    resource = Path(reaper.RPR_GetResourcePath())
    registry = resource / "reaper-kb.ini"
    candidates = set()
    if registry.is_file():
        for line in registry.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = re.match(r'^SCR\s+\d+\s+\d+\s+\S+\s+"[^"]*"\s+(.+?)\s*$', line)
            if not match:
                continue
            path = Path(match[1].strip('"'))
            if path.name.casefold() != "aaf station - export aaf.py":
                continue
            if not path.is_absolute():
                path = resource / "Scripts" / path
            if path.is_file() and (path.parent / "aaf_export_ui.py").is_file():
                candidates.add(path.parent.resolve())
    if len(candidates) == 1:
        return candidates.pop()

    # Match Import's preference when REAPER reports only the script basename.
    for name in ("AAF Station", "reaper-aaf-importer"):
        preferred = (resource / "Scripts" / name).resolve()
        if preferred in candidates:
            return preferred

    result = reaper.RPR_GetUserFileNameForRead("", "Locate aaf_export_ui.py inside AAF Station", "py")
    if not result[0]:
        return None
    helper = Path(result[1])
    if helper.name != "aaf_export_ui.py" or not helper.is_file():
        raise ValueError("Select aaf_export_ui.py from the AAF Station folder.")
    return helper.parent


def _aaf_export():
    folder = package_directory()
    if folder is None:
        return
    sys.path.insert(0, str(folder))
    try:
        from aaf_runtime import external_python
        from reaper_snapshot import read_project
    finally:
        sys.path.remove(str(folder))
    executable = external_python(folder)
    project = read_project(reaper)
    if not any(row["clips"] for row in project["tracks"]):
        raise ValueError("The active REAPER project has no unmuted audio items.")
    temporary = Path(tempfile.mkdtemp(prefix="aaf-station-export-"))
    try:
        snapshot = temporary / "project.json"
        snapshot.write_text(json.dumps(project, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        subprocess.Popen([executable, str(folder / "aaf_export_ui.py"), str(snapshot)],
                         cwd=str(folder), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


try:
    _aaf_export()
except Exception as exc:
    reaper.RPR_ShowMessageBox(str(exc), "AAF Station - Export", 0)
