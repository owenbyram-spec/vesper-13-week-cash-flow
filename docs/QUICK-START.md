# Start here

1. Extract the downloaded repository into a permanent folder.
2. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/) if needed. On Windows, enable the Python launcher when offered.
3. macOS: open `start.command`. Windows: open `start.bat`. The first launch creates this tool's `.venv` and downloads pinned Python packages from PyPI. No administrator access or global package installation is required.
4. Choose the labelled synthetic example. Complete the workflow before adding financial data or credentials.
5. Create a separate workspace for each client/entity. Use the CSV templates or follow [QuickBooks setup](QUICKBOOKS-SETUP.md).
6. Follow [AI setup](MODEL-SETUP.md) to enable the embedded assistant. Normal calculations and exports also work without AI.
7. Keep the launcher open while using the app or automatic refresh. Press Ctrl+C to stop; reopen the same folder to resume.

On macOS, if Finder treats the command file as text, use Terminal: `cd` into the extracted folder and run `python3.12 run.py`. Only run scripts from the source you reviewed. Windows terminal alternative: `py -3.12 run.py`.

Each app is a device-owner local utility. Separate client workspaces are supported; concurrent team logins and enforced preparer/approver identity are outside this build. Verify results against source evidence before using them in professional work.
