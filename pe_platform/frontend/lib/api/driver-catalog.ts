import { apiClient } from "./client";

export type DriverCatalogIssue = { code: string; message: string };

export type DriverCatalogEntry = {
    key: string;
    name: string;
    entity_type: "customer" | "vendor" | "unresolved";
    entity_id: string | null;
    aliases: string[];
    active: boolean | null;
    first_seen: string | null;
    last_seen: string | null;
    gl_line_count: number;
    identity_basis: "gl_id" | "typed_gl" | "unique_name" | "unresolved";
};

export type DriverCatalog = {
    schema_version: 1;
    source: "GeneralLedger";
    company_id: string;
    start_date: string;
    end_date: string;
    observed_at: string;
    coverage: {
        status: "complete" | "partial" | "unavailable";
        issues: DriverCatalogIssue[];
        data_rows: number;
        named_rows: number;
        unattributed_rows: number;
    };
    entries: DriverCatalogEntry[];
};

export type ForecastCatalogRow = {
    row_key: string;
    name: string;
    amounts: Array<number | null>;
    source?: string;
    /** Explicit accounting contact identity, unlike a local row/override key. */
    qbo_id?: string | number | null;
};

export type CatalogAssociation = {
    entry: DriverCatalogEntry;
    row: ForecastCatalogRow | null;
    rowType: "customer" | "vendor" | null;
    matchBasis: "source_id" | "name" | null;
    status: "scheduled" | "no_future_cash" | "not_in_forecast" | "needs_review";
    reason: string | null;
};

function record(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): value is string {
    return typeof value === "string" && value.trim().length > 0;
}

function count(value: unknown): value is number {
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isoDate(value: unknown): value is string {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

/** Reject malformed evidence instead of interpreting it as an empty, complete directory. */
export function validateDriverCatalog(value: unknown, expectedCompanyId: string): DriverCatalog {
    const fail = () => { throw new Error("The GL driver directory response is incomplete or belongs to a different company. Refresh the directory again."); };
    if (!record(value) || value.schema_version !== 1 || value.source !== "GeneralLedger"
        || value.company_id !== expectedCompanyId || !isoDate(value.start_date) || !isoDate(value.end_date)
        || value.start_date > value.end_date || !text(value.observed_at)
        || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value.observed_at)
        || !isoDate(value.observed_at.slice(0, 10)) || !Number.isFinite(Date.parse(value.observed_at))
        || !record(value.coverage) || !Array.isArray(value.entries)) return fail();
    const coverage = value.coverage;
    if (typeof coverage.status !== "string" || !["complete", "partial", "unavailable"].includes(coverage.status)
        || !Array.isArray(coverage.issues) || !coverage.issues.every((issue) => record(issue) && text(issue.code) && text(issue.message))
        || !count(coverage.data_rows) || !count(coverage.named_rows) || !count(coverage.unattributed_rows)
        || coverage.named_rows + coverage.unattributed_rows > coverage.data_rows) return fail();
    const keys = new Set<string>();
    for (const entry of value.entries) {
        if (!record(entry) || !text(entry.key) || keys.has(entry.key) || !text(entry.name)
            || typeof entry.entity_type !== "string" || !["customer", "vendor", "unresolved"].includes(entry.entity_type)
            || !(entry.entity_id === null || text(entry.entity_id))
            || !Array.isArray(entry.aliases) || !entry.aliases.every(text)
            || !(entry.active === null || typeof entry.active === "boolean")
            || !(entry.first_seen === null || isoDate(entry.first_seen))
            || !(entry.last_seen === null || isoDate(entry.last_seen))
            || (typeof entry.first_seen === "string" && (entry.first_seen < value.start_date || entry.first_seen > value.end_date))
            || (typeof entry.last_seen === "string" && (entry.last_seen < value.start_date || entry.last_seen > value.end_date))
            || (typeof entry.first_seen === "string" && typeof entry.last_seen === "string" && entry.first_seen > entry.last_seen)
            || !count(entry.gl_line_count) || entry.gl_line_count === 0 || entry.gl_line_count > coverage.named_rows
            || typeof entry.identity_basis !== "string" || !["gl_id", "typed_gl", "unique_name", "unresolved"].includes(entry.identity_basis)
            || (entry.identity_basis === "gl_id" && entry.entity_id === null)) return fail();
        keys.add(entry.key);
    }
    if (coverage.status !== "unavailable" && value.entries.length === 0 && coverage.named_rows > 0) return fail();
    return value as DriverCatalog;
}

export async function fetchDriverCatalog(companyId: string, signal?: AbortSignal): Promise<DriverCatalog> {
    if (!companyId.trim()) throw new Error("Select the current company before loading GL drivers.");
    const response = await apiClient.get("/cashflow-forecast/driver-catalog", {
        params: { company_id: companyId }, signal,
    });
    return validateDriverCatalog(response.data, companyId);
}

type RowRef = { row: ForecastCatalogRow; type: "customer" | "vendor" };
type Candidate = { entry: DriverCatalogEntry; idRows: RowRef[]; nameRows: RowRef[]; names: string[] };

function nameKey(type: string, name: string) { return `${type}\u0000${name.trim()}`; }
function sourceId(value: ForecastCatalogRow["qbo_id"]): string | null {
    return typeof value === "string" && value.trim() ? value.trim()
        : typeof value === "number" && Number.isSafeInteger(value) ? String(value) : null;
}
function addIndex(index: Map<string, RowRef[]>, key: string, value: RowRef) {
    const values = index.get(key) ?? [];
    values.push(value);
    index.set(key, values);
}

function futureStatus(row: ForecastCatalogRow, lookback: number, weekCount: number): {
    status: "scheduled" | "no_future_cash" | "needs_review"; reason: string | null;
} {
    if (!Number.isSafeInteger(lookback) || !Number.isSafeInteger(weekCount) || lookback < 0 || weekCount <= lookback) {
        return { status: "needs_review", reason: "Future forecast weeks are unavailable." };
    }
    let scheduled = false;
    for (let index = lookback; index < weekCount; index++) {
        const amount = row.amounts[index];
        if (typeof amount !== "number" || !Number.isFinite(amount)) {
            return { status: "needs_review", reason: "This forecast row has unavailable future amounts." };
        }
        if (amount !== 0) scheduled = true;
    }
    return { status: scheduled ? "scheduled" : "no_future_cash", reason: null };
}

export function forecastDriverStatus(row: ForecastCatalogRow, lookback: number, weekCount: number): "scheduled" | "no_future_cash" | "needs_review" {
    return futureStatus(row, lookback, weekCount).status;
}

/**
 * Read-only association, not a financial merge. Manual rows and ambiguous
 * identities stay separate. Local row/override keys never become contact IDs.
 */
export function associateDriverCatalog(
    catalog: DriverCatalog,
    customers: ForecastCatalogRow[],
    vendors: ForecastCatalogRow[],
    lookback: number,
    weekCount: number,
): { entries: CatalogAssociation[]; unlinkedCustomers: ForecastCatalogRow[]; unlinkedVendors: ForecastCatalogRow[] } {
    const rows: RowRef[] = [
        ...customers.map((row): RowRef => ({ row, type: "customer" })),
        ...vendors.map((row): RowRef => ({ row, type: "vendor" })),
    ];
    const byId = new Map<string, RowRef[]>();
    const byName = new Map<string, RowRef[]>();
    for (const item of rows) {
        if (item.row.source === "manual") continue;
        const id = sourceId(item.row.qbo_id);
        if (id !== null) addIndex(byId, nameKey(item.type, id), item);
        addIndex(byName, nameKey(item.type, item.row.name), item);
    }
    const catalogNames = new Map<string, Set<string>>();
    const candidates: Candidate[] = catalog.entries.map((entry) => {
        const names = [...new Set([entry.name, ...entry.aliases].map((value) => value.trim()))];
        for (const name of names) {
            const key = nameKey(entry.entity_type, name);
            const identities = catalogNames.get(key) ?? new Set<string>();
            identities.add(entry.key); catalogNames.set(key, identities);
        }
        return {
            entry, names,
            idRows: entry.entity_id === null ? [] : byId.get(nameKey(entry.entity_type, entry.entity_id)) ?? [],
            nameRows: [...new Set(names.flatMap((name) => byName.get(nameKey(entry.entity_type, name)) ?? []))],
        };
    });
    const needsIdentityReview = (entry: DriverCatalogEntry) => entry.entity_type === "unresolved"
        || entry.identity_basis === "unresolved" || entry.identity_basis === "unique_name";
    const ambiguousNames = new Set(candidates.filter((candidate) => needsIdentityReview(candidate.entry))
        .flatMap((candidate) => candidate.names));
    const idClaims = new Map<RowRef, number>();
    const reservedIds = new Set<RowRef>();
    for (const candidate of candidates) {
        if (needsIdentityReview(candidate.entry)) continue;
        for (const row of candidate.idRows) reservedIds.add(row);
        if (candidate.idRows.length === 1) {
            const row = candidate.idRows[0]; idClaims.set(row, (idClaims.get(row) ?? 0) + 1);
        }
    }
    const used = new Set<RowRef>();
    const entries = candidates.map((candidate): CatalogAssociation => {
        const { entry, idRows, nameRows, names } = candidate;
        const empty: CatalogAssociation = { entry, row: null, rowType: null, matchBasis: null, status: "not_in_forecast", reason: null };
        const review = (reason: string): CatalogAssociation => ({ ...empty, status: "needs_review", reason });
        const match = (item: RowRef, matchBasis: "source_id" | "name"): CatalogAssociation => {
            used.add(item);
            return { ...empty, row: item.row, rowType: item.type, matchBasis, ...futureStatus(item.row, lookback, weekCount) };
        };
        if (needsIdentityReview(entry)) return review("The GL contact identity needs review before associating a forecast row.");
        if (idRows.length > 1) return review("Multiple forecast rows carry this accounting contact ID.");
        if (idRows.length === 1) {
            return idClaims.get(idRows[0]) === 1 ? match(idRows[0], "source_id")
                : review("Multiple GL entries claim the same accounting contact ID.");
        }
        if (nameRows.length === 0) return empty;
        if (nameRows.length > 1) return review("This GL name or alias matches multiple forecast rows.");
        const item = nameRows[0];
        if (typeof item.row.qbo_id === "number" && !Number.isSafeInteger(item.row.qbo_id)) {
            return review("The matching forecast row has an invalid numeric accounting contact ID.");
        }
        const rowId = sourceId(item.row.qbo_id);
        if (entry.entity_id !== null && rowId !== null && entry.entity_id !== rowId) {
            return review("The matching display name carries a different accounting contact ID.");
        }
        if (reservedIds.has(item) || used.has(item)
            || ambiguousNames.has(item.row.name.trim())
            || names.some((name) => (catalogNames.get(nameKey(entry.entity_type, name))?.size ?? 0) > 1)) {
            return review("This name or alias does not identify one unique GL contact and forecast row.");
        }
        return match(item, "name");
    });
    return {
        entries,
        unlinkedCustomers: rows.filter((item) => item.type === "customer" && !used.has(item)).map((item) => item.row),
        unlinkedVendors: rows.filter((item) => item.type === "vendor" && !used.has(item)).map((item) => item.row),
    };
}
