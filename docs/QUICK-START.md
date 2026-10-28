# Start here

## Default: local Python script

1. Extract the repository into a folder you control.
2. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/) if needed. On Windows, enable the Python launcher when offered.
3. In Terminal, change into that folder and run:

```sh
python3.12 forecast.py --demo --out ./exports/first-forecast
```

Windows equivalent: `py -3.12 forecast.py --demo --out .\exports\first-forecast`. The script needs no `.venv`, package download, API key, browser session or open port. It finishes after writing its output.

Open `forecast.csv` in your spreadsheet tool or `forecast.html` as a local file. `forecast.json` preserves exact amounts and hashes; `backup.json` preserves the inputs, source evidence and immutable version. The `workspace` subfolder contains that run's SQLite snapshot. Review the output before relying on it. Use a new output folder for every run; existing folders are never overwritten. Without arguments, `forecast.py` creates a separate dated demo folder under `exports`.

Use `--input my-inputs.json` for your own [canonical JSON inputs](DATA-FORMAT.md). Optional `--delay-days 14` writes `scenario-preview.json` alongside the unchanged baseline. This is a pending preview, not an approved change. No assistant or accounting connection is needed.

## Optional: local browser interface

Choose this path for interactive editing, CSV imports, version comparison, explicit scenario approval, Excel export or an optional assistant. On macOS open `start.command`; on Windows open `start.bat`. Unlike the CLI, first browser launch creates a private `.venv` and downloads pinned packages from PyPI. No administrator access or global package installation is required.

Check the app folder, data directory and local address printed by the launcher. Start with its labelled synthetic example, or restore a CLI `backup.json` into a separate workspace. Pending CLI proposals stay in the backup as evidence; regenerate them against the restored version before approval. Use separate workspaces for each client/entity. Model and QuickBooks connections are separate opt-ins described in [AI setup](MODEL-SETUP.md) and [QuickBooks setup](QUICKBOOKS-SETUP.md).

Keep this launcher open while using the browser tool or optional refresh. Press Ctrl+C to stop; reopen the same folder to resume.

On macOS, if Finder treats the command file as text, use Terminal: `cd` into the extracted folder and run `python3.12 run.py`. Only run scripts from the source you reviewed. Windows terminal alternative: `py -3.12 run.py`.

## Check which browser copy you are using

The default address is `http://127.0.0.1:8766`. If it is occupied, inspect the existing app's entity and Local data & privacy directory before deciding which copy to use. The launcher refuses an occupied port before preparing packages and never stops the other process.

Downloading or extracting another copy does **not** create separate browser-workspace data: each browser launch uses the same OS application-data directory unless `--data-dir` is supplied. Changing only `--port` changes the address, not that directory. Avoid running multiple browser processes against the same directory. The CLI always uses its new output folder instead and never opens this browser data directory.

For an isolated trial, run this from the extracted folder, replacing the example port if needed:

```sh
python3.12 run.py --port 8876 --data-dir ./local-data/trial
```

Windows equivalent: `py -3.12 run.py --port 8876 --data-dir .\local-data\trial`. Reuse the same folder and directory to resume the trial. The launcher prints their resolved paths. Normal default locations are `~/Library/Application Support/Vesper/13-week-cash-flow` on macOS, `%LOCALAPPDATA%\Vesper\13-week-cash-flow` on Windows and `$XDG_DATA_HOME/vesper/13-week-cash-flow` (or `~/.local/share/vesper/13-week-cash-flow`) on Linux.

Before entering credentials, open Connections & setup and check its entity, address and data directory. Connections belong to that local entity. Unsaved form values clear when you close the dialog or change entity. A successful test in another copy or entity does not configure the one currently displayed.

Each app is a device-owner local utility. Separate client workspaces are supported; concurrent team logins and enforced preparer/approver identity are outside this build. Verify results against source evidence before using them in professional work.
