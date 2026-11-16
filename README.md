# AAF Station for REAPER

Import and export AAF audio edits in REAPER. Both actions let you choose tracks and preview the clip layout. Import resolves left and right source channels separately. Export supports a time selection, a named region, or the whole project.

## Install with ReaPack

1. In REAPER, open **Extensions → ReaPack → Import repositories** and paste this URL:

   ```text
   https://raw.githubusercontent.com/gapalil001/aaf-station/main/index.xml
   ```

2. Open **Extensions → ReaPack → Browse packages**, search for **AAF Station**, mark it for installation, and click **Apply**.
3. In **Preferences → Plug-ins → ReaScript**, enable Python. Use Python 3.9 or newer with Tkinter. On macOS, the REAPER and Python builds must have a compatible architecture.
4. In the Action List, run **AAF Station - Import AAF.py** or **AAF Station - Export AAF.py**.

The package includes `pyaaf2`. If REAPER's Python loads but the window cannot find a Python with Tkinter, use `config.example.json` beside the installed scripts as a template for `config.json`.

## Import and export

- **Import** supports linked and embedded PCM, track selection, and manual A1/A2 pairing when the AAF supplies separate mono tracks. Preview opens while the file is being analyzed. Import creates one undo step.
- **Export** writes linked WAV or embedded PCM. The preview shows changes from trimming to the selected range and additional tracks for overlapping clips. Linear fades can be rendered into the audio or kept as metadata for compatible readers.

Verified on REAPER 7.85 for macOS. The package transfers audio edits, not a full mix with plug-ins or automation. Direct import of exported AAF into Pro Tools has not been verified.

## Maintain the index

Update `@version` in the import action for a new release. Commit the source changes, then run `reapack-index --strict` from the repository root and push the source and index commits. The index pins each version to its source commit.

Run the Python tests with `python3 -m unittest discover -s AAF/tests -q`. See [third-party notices](AAF/THIRD_PARTY_LICENSE.txt) for the bundled dependency.
