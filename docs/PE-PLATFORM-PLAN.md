# PE cash-flow platform adoption

## Authority and scope

`primary-record`: Owen requested that this repository use PE Workflow Builder's actual cash-flow platform logic and AI foundation. He clarified the scope as **“Cash-flow workspace with the same platform and AI foundation”**, rather than the whole module catalog.

Allowed writes are confined to this repository: a pinned source kernel under `pe_platform/upstream`, local backend adapters under `pe_platform/backend`, the cash-flow React workspace under `pe_platform/frontend`, a separate launcher/dependency environment, focused tests and setup/provenance documentation. PE Workflow Builder is a read-only source. Its unrelated work-in-progress files, the existing 8876 process/data, other repositories, credentials and credential stores are excluded from modification. No publication, accounting writeback, license terms or live-provider validation is included in this task.

`primary-record`: After local adoption, Owen separately authorized committing/pushing these changes and documentation, followed by a fresh public-ZIP synthetic run without API keys. That publication authorization preserves the same 8876/data, unimplemented Xero, unverified live integrations and no-added-license boundaries.

## Source findings

`repository-observation`: The old utility uses Sprint's exact-money engine, explicit expected dates and full recurring amounts per occurrence. PE uses a Friday-ending grid, historical DSO/DPO, invoice plus inferred run-rate projection, actuals review/close, spreads, variance and export snapshots. These are distinct engine/input contracts. Existing saved versions must not be recalculated or silently converted.

`repository-observation`: The source cash-flow page is a real Next/React workspace. Native assistant behavior includes persistent scoped conversations, policy/tool metadata, streaming, ProposedAction drafts, confirmation fingerprints, numeric-evidence metadata and run history. Importing only its projection helper or reskinning the existing form would not meet the request.

`repository-observation`: Shared models and tool handlers create a large transitive backend dependency graph. Source modules required by that graph may remain in the kernel, but only cash-flow and supporting assistant/platform APIs and UI are exposed. Journal entries, accounting writeback, invoice outreach and the broader module catalog are outside the local surface.

## Implementation

1. Pin the source Git commit and record every copied path/blob/content hash. Preserve business code byte-for-byte where possible; identify local adapter changes explicitly.
2. Mount the native cash-flow backend contracts and models with a separate local database. Use server-owned local principals/workspace binding, native tenant checks and explicit local session/CSRF checks. Keep existing legacy CLI/browser files and saved versions operational.
3. Use the native cash-flow React page and assistant/platform components, with a local shell, workspace/connection setup and same-origin API proxy. Guard company changes and assistant streams against stale scope. Restrict numeric inputs to representable cents rather than rounding incompatible legacy amounts.
4. Bind the native assistant runtime to workspace-owned credentials and a cash-flow-scoped registry. Persist conversations/proposals/run evidence. Keep proposal-first financial review; no unattended financial writes or global-key/provider fallback.
5. Provide a separately launched synthetic workspace using a labelled local source fixture. External QBO/model requests require explicit user configuration. Keep live integrations, real credential-store behavior, Windows and broad accounting formats unverified until actually exercised.
6. Verify native source parity, Friday/cadence/source/actuals/scenario/export behavior, scope/stale controls, proposal review and streaming with fixtures; typecheck/build the workspace and perform an isolated browser journey. Report exact gaps rather than claiming complete parity from copied files alone.

## Acceptance and stop conditions

- The cash-flow page and financial business modules originate from the pinned PE source, with a checkable manifest.
- The local app runs without the original PE checkout or its services/data. Infrastructure adaptations are documented rather than called source-identical.
- The same forecast, actuals, spread, scenario/export and governed assistant contracts run through the native implementation.
- Existing legacy versions and the 8876 data remain unchanged. Legacy explicit occurrences never enter an inferred monthly-average field.
- Credentials stay scoped and excluded from exports; source/company changes cannot authorize stale writes or reuse another workspace's conversation.
- Only the cash-flow/platform surface is mounted. Source-only dependencies do not imply exposed module/catalog features.
- Failure to preserve tenant/approval/secret controls, incomplete acquisitions or missing provider/host access remain explicit stop branches for the dependent feature. No source-platform or external-state mutation is authorized.

`repository-observation`: Local execution, source identity, fixture-based financial/assistant controls, production frontend build and the isolated browser workflow are recorded in [Verification](VERIFICATION.md). Those checks establish the exercised local paths; they do not establish complete hosted-platform parity. Live provider/QBO acquisition, actual credential storage, Windows and broad accounting formats remain open gates.
