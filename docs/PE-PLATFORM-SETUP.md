# Run the PE-native cash-flow workspace

This edition uses actual PE Workflow Builder cash-flow code, its React workspace and its governed assistant runtime. The source is pinned in [the backend manifest](../pe_platform/source-manifest.json), [frontend manifest](../pe_platform/frontend/SOURCE-MANIFEST.json) and [adoption plan](PE-PLATFORM-PLAN.md). Local adapters provide device-owned identity, workspace binding, credential storage, a same-origin proxy and stricter proposal review. They are explicit adaptations, not a claim that hosted PE infrastructure has been reproduced or validated.

## Install and start

1. Install Python 3.12 or newer and Node.js 20 or newer. Python 3.12.13 and Node 24.16.0 are available on the development host; first-time installation and Windows execution remain unverified.
2. Open Terminal in this repository and run:

```sh
python3.12 start_platform.py --data-dir ./local-data/pe-platform
```

The first launch installs the platform's hash-locked Python requirements into `.venv-platform`, runs `npm ci` against the frontend lockfile with dependency scripts disabled, and builds the native browser workspace. This edition requires packages; the older dependency-free CLI remains a separate legacy tool.

The browser defaults to `http://127.0.0.1:8770`; the API defaults to `http://127.0.0.1:8771`. Both bind to loopback. The launcher checks for occupied ports and stops no existing process. Keep it open. Ctrl+C stops only its own children, retaining saved data. After preparation, `--no-install` refuses missing or mismatched dependencies/builds rather than downloading anything.

For another isolated instance choose a free browser/API pair and a new data directory:

```sh
python3.12 start_platform.py --port 8780 --api-port 8781 --data-dir ./local-data/another-pe-trial
```

Each copy otherwise uses the same default platform data directory: `~/Library/Application Support/Vesper/pe-cash-flow` on macOS, `%LOCALAPPDATA%\Vesper\pe-cash-flow` on Windows, or the XDG application-data directory on Linux. A new port alone does not isolate data. An OS-backed lock refuses a second platform process using the same directory. The launcher refuses legacy directories containing `forecast.sqlite3`; it never migrates or replaces the existing 8876 data.

## First review

Choose the explicitly labelled synthetic demo. Its local source fixture drives the actual native cash-flow acquisition/projection/actuals code; it is not a QuickBooks sandbox connection or a real model response. No API key is required to inspect the financial workspace.

Review the Friday-ending forecast and lookback column, bank/control anchor, source warnings, customer/vendor drivers, day and monthly-average assumptions, cell overrides, balance spreads and actuals statuses. Use the native actuals pull/categorize/close workflow and reconcile the result. A partial source must remain visible rather than becoming a complete zero balance. Capture/download the selected immutable export snapshot and check the company and snapshot identity.

The forecast fills the working canvas. Choose **Drivers** above the grid to read the customer/vendor names observed in the selected company's **General Ledger**, using the forecast's **180-day lookback**. The displayed date range defines the directory's coverage. GL names remain visible even when they have no forecast row; inactive contacts are included when observed in the GL. Customer/vendor metadata resolves source identities and never adds unused master-list names to the directory. Ambiguous or unresolved identities remain marked for review.

Each GL name has a forecast status. A name's presence in the GL does not create a forecast row, a recurring assumption or a zero-dollar estimate. Name-based links are identified separately from accounting-ID links; duplicate identities are not silently merged. Manual and other unlinked forecast rows remain in a separate section. Search covers the directory's names and aliases; any local pagination shows the available count. **Refresh GL directory** rereads the directory without refreshing or editing the financial forecast. Incomplete reads remain visible; a failed refresh retains the last successful directory for the same workspace with an explicit warning.

The info control beside a forecast entity still opens that exact row's full name, timing, monthly run-rate, available source evidence and future weekly amounts. The read-only drawer overlays the right side without narrowing the forecast. Close it with Escape, the close button or the backdrop. Edit values in the grid using the existing controls. Changing workspaces closes the drawer, clears its selection and discards the previous company's directory. The labelled synthetic workspace provides fictional GL observations without credentials; it does not establish live GL acquisition.

Native history-derived **monthly run-rates** are not legacy per-occurrence amounts. PE's cadence conversion determines the payments placed from those run-rates. Explicit schedules/overrides are separate. Do not enter a legacy $1,000 per-payday amount into a $1,000 monthly-average field. The native UI/export contract is currently USD and representable cents; mixed currencies, nonfinite/excess-precision or unsafe numeric values are refused rather than silently converted.

The local finite-spread adapter retains PE's cent allocation while preventing a finite schedule from becoming a recurring monthly run-rate. Three weekly periods totaling $1,200 create exactly three $400 buckets. Monthly weekend dates map to the next Friday bucket; schedules extending beyond the visible horizon and names colliding with existing source/assumption rows are refused for renewed review. Refresh preserves the explicit schedule. This is an identified local correction to the pinned source behavior.

## Assistant and review

The assistant is **Vesper Bot**. Its visible presentation, prompts, error/report text and internal namespace use that identity. The manifests retain original source identities alongside hashes of the explicit branding adaptations; deterministic cash-flow modules remain unchanged. The old unexposed catalog's branded protocol markers are not migrated in existing saved records.

**Connections** accepts a workspace-owned OpenAI or Anthropic key and model ID in masked fields, with explicit data-flow consent and session-only storage by default. Optional persistence uses the existing OS credential-store abstraction with no plaintext fallback. A new workspace does not inherit another's connection. Saving is separate from a small provider connection test, and both are separate from an actual scoped assistant response. Enter keys only in this app; never put them in chat, fixtures, source or input files.

The local integration uses PE's native context/policy/tool/runtime, conversations, typed evidence/numeric-verification metadata, ProposedAction ledger and streaming events. The selected tools are cash-flow reads/analysis/refresh, cash-flow drafts and supporting memory. Journal entries, accounting writeback, invoice outreach, broad financial packs and unpopulated finance-search indexes are not offered. There is no implicit global-key or cross-provider fallback. Semantic embedding acquisition is not enabled; supported context/memory retrieval remains explicitly scoped.

Model-generated financial changes remain proposals. Open **Proposal review**, inspect the exact payload and deterministic before/after preview, then explicitly approve the reviewed fingerprint/token. A changed forecast or workspace invalidates the review. Approval captures reproducible native export envelopes; the latest serve cache and current-week accuracy cache retain their native update semantics. Historical saved exports remain immutable. Human-directed grid edits use the native permissioned UI; this local utility does not establish independent preparer/approver identity.

Assistant streams/history belong to the selected workspace and financial base. Switching workspace aborts stale streams and clears caches. Successful financial mutations start a current conversation for the new base; native history and rolling-summary retrieval use that conversation ID. Earlier conversations/messages remain saved for audit, while their figures are excluded from the current view and next request. They cannot approve a stale proposal.

The provider-neutral adapter uses the SDK's explicit client credentials and streaming interface; [official OpenAI Python documentation](https://developers.openai.com/api/reference/python) describes that interface. The implementation retains the source kernel's pinned SDK/protocol and uses fixture clients for verification. No live account/model availability is implied.

## QuickBooks and data

Create your own USD entity before connecting a real company. In **Connections**, enter the QBO environment, Client ID, Client secret, Realm ID and refresh token obtained using [the Intuit authorization procedure](QUICKBOOKS-SETUP.md). Save, test, inspect the returned company and explicitly confirm it for this workspace. No financial source is selected solely by saving a token. The API transport is read-only; unsupported reports/methods and workspace/currency mismatches are refused. Live acquisition remains unverified.

The Drivers directory adds a fixed read of the **GeneralLedger** report and paged customer/vendor identity metadata for that confirmed company. Both active and inactive contacts must be requested explicitly; QBO queries return complete contact objects rather than selected-field projections. The adapter exposes the GL-observed names and minimal identity/coverage information in the drawer. See Intuit's [query limitations and inactive-contact rules](https://help.developer.intuit.com/s/article/SQL-Queries) and [General Ledger API reference](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/generalledger). Report observations are not an atomic accounting snapshot. Unsupported or incomplete report shapes cannot establish complete coverage, and live General Ledger acquisition remains unverified.

The platform database, source observations and workspace selection live under the printed platform data directory. Credentials remain session/OS-store scoped and separate from financial snapshots. Protect local data, downloads and backups as business information. Back up only a consistent local database/data directory; copying finance records does not transfer credentials. No Vesper telemetry or hosted finance backend is added.

The source shared models/services have a broad transitive dependency graph. Only the cash-flow and supporting assistant/platform routes are mounted. Local SQLite uses native SQLAlchemy models and tenant/realm checks; it does not establish Postgres row-level-policy or hosted identity behavior. No Redis/Celery service or unattended background objectives are started by this launcher; local supported refresh work is bounded to the running process.

## Legacy records and verification limits

`forecast.py`, `run.py`, `start.command` and their original exact-money data formats remain the legacy path. Existing approved versions and backups remain readable there, with their original hashes and calculation rules. Nothing automatically converts them into PE's Friday/inferred-run-rate contract. The old $35,000/$15,500 synthetic results verify that legacy workflow, not the native PE grid.

See [Verification](VERIFICATION.md) for checks actually performed. Live model access, real QBO sandbox/production acquisition, actual OS credential-store behaviour, Windows execution and broad accounting formats remain unverified. **Xero API acquisition is not implemented.** Source manifests establish code origin; copied code and passing fixtures alone do not establish complete runtime parity, production readiness or hosted control behavior. No software license has been added.
