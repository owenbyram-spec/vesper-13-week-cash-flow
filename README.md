# Vesper & Co — 13-Week Cash Flow

A standalone local finance tool designed for **fractional CFOs**. Build a reproducible 13-week cash forecast, review timing assumptions, and compare approved scenarios.

The assistant is built into the app. Financial calculations are deterministic; AI reads scoped results and drafts proposals for explicit human review.

## Run locally

Install **Python 3.12+** from [python.org](https://www.python.org/downloads/), extract this repository, then open **start.command** on macOS or **start.bat** on Windows. First launch downloads pinned packages into this folder's `.venv`.

Terminal alternative:

```sh
python3.12 run.py
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766) if the browser does not open. Keep the launcher running. Begin with the labelled synthetic example; no credentials are needed for that financial workflow.

Check the launcher's app folder, data directory and local address before entering client data or credentials. Another downloaded copy uses the same default data directory. For an isolated trial, choose both a free port and an explicit `--data-dir`; see [Quick start](docs/QUICK-START.md). Connections & setup displays the current entity, address and data directory.

## Your first useful result

1. Run the synthetic example and inspect its inputs and controls.
2. Create a separate client/entity workspace.
3. Import your own CSVs or connect your own QuickBooks developer app.
4. Review cash timing, recurring amounts per occurrence and missing input coverage.
5. Configure your own model API key for real embedded AI; synthetic rehearsal is labelled separately.
6. Save, review and export the completed result.

## Setup and controls

- [Quick start](docs/QUICK-START.md)
- [Model API-key setup](docs/MODEL-SETUP.md)
- [QuickBooks authorization and refresh](docs/QUICKBOOKS-SETUP.md)
- [Data formats and financial controls](docs/DATA-FORMAT.md)
- [Data flow, credentials and recovery](docs/DATA-AND-PRIVACY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Verification and limitations](docs/VERIFICATION.md)
- [Source provenance](docs/PROVENANCE.md)
- [Design notes](docs/ARCHITECTURE.md)

## Boundaries

This is a single-device utility with separate local workspaces. It has no shared team login, accounting writeback, Vesper-hosted backend or automatic model-provider fallback. Automatic accounting refresh runs only while the local process is running. Model usage is billed to your own provider account.

Live provider access and Windows execution remain unverified; check the verification record for this build. Source repository: [vesper-13-week-cash-flow](https://github.com/owenbyram-spec/vesper-13-week-cash-flow). No software license has been added at this stage; [the release checklist](RELEASE-CHECKLIST.md) records publication verification.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Windows uses `.venv\Scripts\python.exe` in the same commands. Tests use synthetic records and injected credentials, not live client data.

Vesper & Co builds tailored finance workflows. Use your existing Vesper point of contact to discuss broader workflows or integrations; this repository does not collect leads or transmit financial data to Vesper.
