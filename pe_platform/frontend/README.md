# Local cash-flow platform frontend

This frontend reuses the actual cash-flow workspace and assistant presentation from PE Workflow Builder at `6667bc5b73173d83400a8d269c23f30942e4f00a`. `SOURCE-MANIFEST.json` records the exact source files and the explicit local adapters. The broader module catalog, SaaS identity and billing interfaces are excluded.

For normal setup, follow [the complete platform guide](../../docs/PE-PLATFORM-SETUP.md). From the repository root, this command prepares the locked dependencies and starts both local processes with an explicit, separate data directory:

```sh
python3.12 start_platform.py --data-dir ./local-data/pe-platform
```

Python 3.12+ and Node.js 20+ must already be installed. All required application source is included here; a PE Workflow Builder checkout is not required. Choose **Load synthetic example** in the browser to inspect the native financial workspace without API keys. This edition installs packages; the dependency-free legacy CLI is a separate path.

For frontend development, start only the backend from the repository root in one terminal:

```sh
python3.12 start_platform.py --backend-only --data-dir ./local-data/pe-platform
```

Then run these commands from `pe_platform/frontend` in a second terminal:

```sh
npm ci --ignore-scripts --no-audit --no-fund
NEXT_TELEMETRY_DISABLED=1 BACKEND_URL=http://127.0.0.1:8771 npm run dev -- --hostname 127.0.0.1 --port 8770
```

Open `http://127.0.0.1:8770`. The same-origin `/bff` proxy forwards only to the configured loopback backend and preserves the backend session, actual Origin, CSRF and displayed-workspace binding. `BACKEND_URL` defaults to `http://127.0.0.1:8771`; `NEXT_PUBLIC_BACKEND_URL` is a compatibility fallback. Provider keys belong only in the dedicated local connection form and never in build variables or browser storage. Connections visibly reports locked/unavailable credential storage or uncertainty about a previously saved OS copy; a failed persistent save keeps the masked draft and does not enable a connection test. Production response headers restrict browser connections/images to the local origin (data images are allowed); assistant Markdown cannot auto-load a remote image. SSE and report downloads continue through the same-origin proxy. Development HMR permits loopback WebSockets only.

For a production build:

```sh
NEXT_TELEMETRY_DISABLED=1 npm run build
HOSTNAME=127.0.0.1 PORT=8770 BACKEND_URL=http://127.0.0.1:8771 npm start
```

The native workspace currently uses **USD** and the source platform's Friday-ending cash calendar. It does not reinterpret the legacy CLI's snapshots. Existing 8876/data stays separate; do not start this frontend or backend on 8876 or reuse its data directory.

The platform shell includes Cash Flow, Assistant, Proposal review and Connections. Company scope comes from the local backend. Switching workspaces clears native page state/query caches and aborts in-flight assistant streams. Successful financial mutations and proposal approvals reset the assistant conversation and abort its old streams without remounting the native grid; failed or non-financial requests retain valid chat context. Model proposals require an actual deterministic forecast preview and explicit human approval of its exact token. Missing/stale previews disable approval.

Verification commands:

```sh
npm run typecheck
npm test
NEXT_TELEMETRY_DISABLED=1 npm run build
```

Tests use synthetic responses. Live model/QBO access, actual OS credential-store behaviour and Windows execution remain separate verification gates. Xero API acquisition remains unimplemented. No software license has been added.
