# @noindex
"""Run with the external Python you want the importer to use."""
import json
from pathlib import Path
import sys
import sysconfig


def main():
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9+ is required")
    import tkinter
    folder = Path(__file__).resolve().parent
    configuration = folder / "config.json"
    configuration.write_text(json.dumps({"python": sys.executable}, indent=2) + "\n", encoding="utf-8")
    print("Saved:", configuration)
    print("External Python:", sys.executable)
    print("Tk:", tkinter.TkVersion)
    version = "%d.%d" % sys.version_info[:2]
    libraries = []
    libdir = Path(sysconfig.get_config_var("LIBDIR") or Path(sys.executable).parent)
    libraries += list(libdir.glob("libpython" + version + "*.dylib"))
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    prefix = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")
    if framework and prefix:
        libraries.append(Path(prefix) / (framework + ".framework") / "Versions" / version / framework)
    libraries += list(Path(sys.executable).parent.glob("python*.dll"))
    print("\nCandidates for REAPER > Preferences > Plug-ins > ReaScript:")
    for path in dict.fromkeys(libraries):
        if path.is_file():
            print("  Library directory:", path.parent)
            print("  Library filename: ", path.name)
    print("\nEnable Python in REAPER separately. Match Python/REAPER architecture.")


if __name__ == "__main__":
    main()
