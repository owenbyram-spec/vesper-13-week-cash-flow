# Data, credentials and recovery

This page describes legacy CLI/browser storage and backup formats. The current PE-native workspace has separate platform databases, source observations and immutable export snapshots. See [PE-native setup](PE-PLATFORM-SETUP.md) for its data, connection and review boundaries. Legacy JSON backup/restore does not apply to the native platform database.

The CLI writes only to its chosen new output folder, including reports, JSON backup and a `workspace/forecast.sqlite3` snapshot. It uses no server, browser, credential store or provider connection. The optional browser tool stores financial workspaces in the operating system's application-data directory by default; a custom `--data-dir` can be supplied explicitly. Back up/export data to a location you control. Exported financial files may contain sensitive business information and are your responsibility to protect.

All browser copies share that default directory. A different port alone does not isolate their data. Check the browser launcher's resolved app/data paths and Connections & setup's entity/address/directory before entering credentials; use a separate `--data-dir` for an isolated browser trial. Session-only credentials last only in that running browser backend process. Connections and persistent credential namespaces are bound to the local data directory and entity; moving a finance backup does not transfer credentials. CLI output folders are separate and contain no connections.

The backend binds to `127.0.0.1`. Host, browser origin, session and CSRF checks protect local API requests. This is a single-device utility; it is not hosted team software. Local user access and disk/backups still require normal device protection.

Model keys, Intuit credentials and tokens use the operating system credential store when explicitly requested, otherwise session memory. No plaintext persistence fallback is used. Local connection metadata records only hashed names of credential entries known absent; it contains no keys or tokens and is excluded from finance backups. A locked credential store must be unlocked before an existing saved credential can be removed. Do not commit workspaces, exports, `.env` files or credentials. The `.gitignore` is a convenience and is not a substitute for inspecting files before publishing.

Explicit real assistant requests transmit the current prompt and permitted workflow evidence to the selected model provider. Intuit requests transmit the connection credentials/authorized requests to Intuit. There is no Vesper telemetry, central storage, shared key or silent cross-provider fallback.

Imports, model output and descriptions are untrusted data. They cannot select another workspace, access credentials, execute code or approve proposals. Financial calculations remain deterministic. Review and approval here records a local operator action; typed names do not establish independently verified preparer/approver identities.

Use versioned JSON export/restore for a complete workflow backup and CSV/Excel/printable exports for review. Restoring a backup requires validation and must preserve money, provenance and scope. Keep originals until you verify a restored copy. Secrets are excluded from all backup/export formats.
