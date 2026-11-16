# @description AAF Station: AAF import and export
# @version 0.2.2
# @author AAF Station
# @changelog First ReaPack package for AAF Station.
# @about
#   Import and export AAF audio edits with track selection and a timeline preview.
#   Requires REAPER with Python ReaScript enabled and a Python 3.9+ installation with Tkinter.
# @provides
#   [main] AAF Station - Export AAF.py
#   aaf_core.py
#   aaf_export.py
#   aaf_export_ui.py
#   aaf_runtime.py
#   aaf_theme.py
#   aaf_ui.py
#   configure_python.py
#   pcm_audio.py
#   reaper_apply.py
#   reaper_snapshot.py
#   config.example.json
#   THIRD_PARTY_LICENSE.txt
#   vendor/pyaaf2-1.7.1-py2.py3-none-any.whl

"""ReaPack installs this action with its companion files and the vendor directory.
"""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import reaper_python as _reaper
from reaper_python import *

_aaf_process = None
_aaf_temp = None
_aaf_log = None
_aaf_last_poll = 0


def _aaf_package_directory():
    """Find this action's companions when REAPER supplies only a script name."""
    compiled = Path(_aaf_start.__code__.co_filename)
    names = {"aaf station - import aaf.py", compiled.name.casefold()}

    def package_at(path):
        path = Path(path).expanduser()
        if all((path / name).is_file() for name in ("aaf_ui.py", "aaf_runtime.py", "reaper_apply.py")):
            return path.resolve()
        return None

    # Standard Python and some REAPER launch modes provide a full path.
    for value in (globals().get("__file__"), str(compiled), *sys.argv[:1]):
        if value:
            path = Path(value).expanduser()
            if path.is_absolute() and path.name.casefold() in names:
                folder = package_at(path.parent)
                if folder:
                    return folder

    # Python ReaScript has no get_action_context. The action registry records
    # installed scripts as absolute paths or relative to REAPER's Scripts folder.
    resource = Path(RPR_GetResourcePath())
    try:
        entries = (resource / "reaper-kb.ini").read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        entries = ()
    registered = set()
    for line in entries:
        match = re.match(r'^SCR\s+\d+\s+\d+\s+\S+\s+(?:"[^"]*"|\S+)\s+(.+?)\s*$', line)
        if not match:
            continue
        script = Path(match[1].removeprefix('"').removesuffix('"'))
        if script.name.casefold() not in names:
            continue
        if not script.is_absolute():
            script = resource / "Scripts" / script
        if script.is_file():
            folder = package_at(script.parent)
            if folder:
                registered.add(folder)
    if len(registered) == 1:
        return registered.pop()

    # REAPER can supply only the basename. Prefer the expected install folders
    # when both a renamed package and the original folder are registered.
    for name in ("AAF Station", "reaper-aaf-importer"):
        preferred = (resource / "Scripts" / name).resolve()
        if preferred in registered:
            return preferred

    # A newly registered or differently named install may need a manual lookup.
    chosen = RPR_GetUserFileNameForRead("", "Locate aaf_ui.py inside AAF Station", "py")
    if not chosen[0]:
        return None
    helper = Path(chosen[1])
    folder = package_at(helper.parent)
    if helper.name.casefold() != "aaf_ui.py" or not folder:
        raise ValueError("Select aaf_ui.py from the fully extracted AAF Station folder.")
    return folder


def _aaf_message(text):
    RPR_ShowMessageBox(str(text), "AAF Station - Import", 0)


def _aaf_choose_file():
    title = "AAF Station - Import AAF"
    if hasattr(_reaper, "RPR_GetUserFileName") and RPR_APIExists("GetUserFileName"):
        result = RPR_GetUserFileName(1, title, "", "AAF files|*.aaf;*.AAF|All files|*.*", "", 4096)
        return result[5] if result[0] else None
    result = RPR_GetUserFileNameForRead("", title, "")
    return result[1] if result[0] else None


def _aaf_cleanup():
    global _aaf_process, _aaf_log, _aaf_temp
    if _aaf_process is not None and _aaf_process.poll() is None:
        _aaf_process.terminate()
    if _aaf_log is not None:
        _aaf_log.close()
        _aaf_log = None
    if _aaf_temp is not None:
        shutil.rmtree(_aaf_temp, ignore_errors=True)
        _aaf_temp = None


def _aaf_poll():
    global _aaf_last_poll
    now = time.monotonic()
    if now - _aaf_last_poll < 0.15:
        RPR_defer("_aaf_poll()")
        return
    _aaf_last_poll = now
    if _aaf_process.poll() is None:
        RPR_defer("_aaf_poll()")
        return
    try:
        result_path = Path(_aaf_temp) / "result.json"
        if not result_path.exists():
            log = (Path(_aaf_temp) / "helper.log").read_text(encoding="utf-8", errors="replace")
            raise RuntimeError("AAF interface exited without a result.\n\n" + log[-5000:])
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["status"] == "cancelled":
            return
        if result["status"] != "ready":
            raise RuntimeError(result.get("message", "AAF preparation failed"))
        spec = importlib.util.spec_from_file_location("reaper_apply", _aaf_folder / "reaper_apply.py")
        apply = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(apply)
        tracks, clips = apply.apply_plan(_reaper, result["plan"], _aaf_project)
        plan = result["plan"]
        message = "Imported %d tracks / %d clips.\nUndo: one step." % (tracks, clips)
        if plan.get("warnings"):
            message += "\n\nDry-import warnings:\n" + "\n".join(plan["warnings"])
        _aaf_message(message)
    except Exception:
        _aaf_message(traceback.format_exc())
    finally:
        _aaf_cleanup()


def _aaf_start():
    global _aaf_process, _aaf_temp, _aaf_log, _aaf_project, _aaf_folder
    try:
        folder = _aaf_package_directory()
        if folder is None:
            return
        _aaf_folder = folder
        spec = importlib.util.spec_from_file_location("aaf_runtime", folder / "aaf_runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        executable = runtime.external_python(folder)
        chosen = _aaf_choose_file()
        if not chosen:
            return
        source = Path(chosen)
        if not source.is_file():
            raise ValueError("AAF file does not exist")
        _aaf_project = RPR_EnumProjects(-1, "", 4096)[0]
        cursor = RPR_GetCursorPositionEx(_aaf_project)
        project_offset = RPR_GetProjectTimeOffset(_aaf_project, False)
        _aaf_temp = tempfile.mkdtemp(prefix="reaper_aaf_")
        result = str(Path(_aaf_temp) / "result.json")
        _aaf_log = open(Path(_aaf_temp) / "helper.log", "wb")
        _aaf_process = subprocess.Popen([executable, str(folder / "aaf_ui.py"), "--file", str(source),
                                        "--result", result, "--cursor", str(cursor),
                                        "--project-offset", str(project_offset)],
                                       cwd=str(folder), stdout=_aaf_log, stderr=_aaf_log,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        RPR_atexit("_aaf_cleanup()")
        RPR_defer("_aaf_poll()")
    except Exception:
        _aaf_cleanup()
        _aaf_message(traceback.format_exc())


_aaf_start()
