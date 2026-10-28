# Start here

1. Extract the downloaded repository into a permanent folder.
2. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/) if needed. On Windows, enable the Python launcher when offered.
3. macOS: open `start.command`. Windows: open `start.bat`. The first launch creates this tool's `.venv` and downloads pinned Python packages from PyPI. No administrator access or global package installation is required.
4. Check the app folder, data directory and local address printed by the launcher. Choose the labelled synthetic example. Complete the workflow before adding financial data or credentials.
5. Create a separate workspace for each client/entity. Use the CSV templates or follow [QuickBooks setup](QUICKBOOKS-SETUP.md).
6. Follow [AI setup](MODEL-SETUP.md) to enable the embedded assistant. Normal calculations and exports also work without AI.
7. Keep the launcher open while using the app or automatic refresh. Press Ctrl+C to stop; reopen the same folder to resume.

On macOS, if Finder treats the command file as text, use Terminal: `cd` into the extracted folder and run `python3.12 run.py`. Only run scripts from the source you reviewed. Windows terminal alternative: `py -3.12 run.py`.

## Check which copy you are using

The default address is `http://127.0.0.1:8766`. If it is occupied, inspect the existing app's entity and Local data & privacy directory before deciding which copy to use. The launcher refuses an occupied port before preparing packages and never stops the other process.

Downloading or extracting another copy does **not** create separate financial data: each copy uses the same OS application-data directory unless `--data-dir` is supplied. Changing only `--port` changes the address, not that directory. Avoid running multiple processes against the same directory.

For an isolated trial, run this from the extracted folder, replacing the example port if needed:

```sh
python3.12 run.py --port 8876 --data-dir ./local-data/trial
```

Windows equivalent: `py -3.12 run.py --port 8876 --data-dir .\local-data\trial`. Reuse the same folder and directory to resume the trial. The launcher prints their resolved paths. Normal default locations are `~/Library/Application Support/Vesper/13-week-cash-flow` on macOS, `%LOCALAPPDATA%\Vesper\13-week-cash-flow` on Windows and `$XDG_DATA_HOME/vesper/13-week-cash-flow` (or `~/.local/share/vesper/13-week-cash-flow`) on Linux.

Before entering credentials, open Connections & setup and check its entity, address and data directory. Connections belong to that local entity. Unsaved form values clear when you close the dialog or change entity. A successful test in another copy or entity does not configure the one currently displayed.

Each app is a device-owner local utility. Separate client workspaces are supported; concurrent team logins and enforced preparer/approver identity are outside this build. Verify results against source evidence before using them in professional work.
