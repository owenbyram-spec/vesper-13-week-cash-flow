import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "./client";
import {
    associateDriverCatalog, fetchDriverCatalog, forecastDriverStatus, validateDriverCatalog,
    type DriverCatalog, type DriverCatalogEntry, type ForecastCatalogRow,
} from "./driver-catalog";

vi.mock("./client", () => ({ apiClient: { get: vi.fn() } }));

function entry(changes: Partial<DriverCatalogEntry> = {}): DriverCatalogEntry {
    return {
        key: "customer:200", name: "Synthetic Alder", entity_type: "customer", entity_id: "200",
        aliases: [], active: true, first_seen: "2026-06-01", last_seen: "2026-10-28",
        gl_line_count: 3, identity_basis: "gl_id", ...changes,
    };
}

function catalog(entries: DriverCatalogEntry[] = [entry()]): DriverCatalog {
    const count = entries.reduce((total, item) => total + item.gl_line_count, 0);
    return {
        schema_version: 1, source: "GeneralLedger", company_id: "realm-a", start_date: "2026-05-01",
        end_date: "2026-10-28", observed_at: "2026-10-28T15:30:00+00:00",
        coverage: { status: "complete", issues: [], data_rows: count, named_rows: count, unattributed_rows: 0 }, entries,
    };
}

function row(changes: Partial<ForecastCatalogRow> = {}): ForecastCatalogRow {
    return { row_key: "forecast-a", name: "Synthetic Alder", source: "hybrid", amounts: [9999, 0, 400, 0], ...changes };
}

beforeEach(() => { vi.clearAllMocks(); });

describe("scoped GL catalog transport", () => {
    it("only performs the scoped GET, preserves coverage and forwards cancellation", async () => {
        const data = catalog();
        data.coverage = { ...data.coverage, status: "partial", issues: [{ code: "page_failed", message: "One source page was unavailable." }] };
        vi.mocked(apiClient.get).mockResolvedValue({ data });
        const controller = new AbortController();
        expect(await fetchDriverCatalog("realm-a", controller.signal)).toBe(data);
        expect(apiClient.get).toHaveBeenCalledExactlyOnceWith("/cashflow-forecast/driver-catalog", {
            params: { company_id: "realm-a" }, signal: controller.signal,
        });
    });

    it("rejects a different company's evidence without converting it to an empty directory", async () => {
        vi.mocked(apiClient.get).mockResolvedValue({ data: { ...catalog(), company_id: "realm-b" } });
        await expect(fetchDriverCatalog("realm-a")).rejects.toThrow("different company");
        expect(apiClient.get).toHaveBeenCalledOnce();
    });

    it.each([
        (data: DriverCatalog) => ({ ...data, schema_version: 2 }),
        (data: DriverCatalog) => ({ ...data, source: "CustomerMaster" }),
        (data: DriverCatalog) => ({ ...data, coverage: undefined }),
        (data: DriverCatalog) => ({ ...data, coverage: { ...data.coverage, status: "complete", named_rows: 4 } }),
        (data: DriverCatalog) => ({ ...data, coverage: { ...data.coverage, data_rows: -1 } }),
        (data: DriverCatalog) => ({ ...data, coverage: { ...data.coverage, data_rows: 0, named_rows: 0 } }),
        (data: DriverCatalog) => ({ ...data, coverage: { ...data.coverage, unattributed_rows: 1 } }),
        (data: DriverCatalog) => ({ ...data, entries: [] }),
        (data: DriverCatalog) => ({ ...data, entries: [data.entries[0], data.entries[0]] }),
        (data: DriverCatalog) => ({ ...data, entries: [entry({ gl_line_count: 0 })] }),
        (data: DriverCatalog) => ({ ...data, entries: [entry({ entity_id: null, identity_basis: "gl_id" })] }),
        (data: DriverCatalog) => ({ ...data, entries: [entry({ first_seen: "2026-04-30" })] }),
        (data: DriverCatalog) => ({ ...data, entries: [entry({ last_seen: "2026-10-29" })] }),
        (data: DriverCatalog) => ({ ...data, start_date: "2026-02-31" }),
        (data: DriverCatalog) => ({ ...data, observed_at: "2026-10-28" }),
    ])("rejects malformed/incomplete or unobserved evidence %#", (change) => {
        expect(() => validateDriverCatalog(change(catalog()), "realm-a")).toThrow("incomplete");
    });

    it("accepts a genuinely complete empty read and explicit unavailable coverage", () => {
        const empty = catalog([]);
        expect(validateDriverCatalog(empty, "realm-a")).toBe(empty);
        empty.coverage = { ...empty.coverage, status: "unavailable", issues: [{ code: "source_error", message: "GL could not be acquired." }] };
        expect(validateDriverCatalog(empty, "realm-a").coverage.status).toBe("unavailable");
    });

    it("preserves unavailable counts after GL parsing without claiming a complete empty directory", () => {
        const unavailable = { ...catalog(), entries: [], coverage: {
            ...catalog().coverage, status: "unavailable" as const,
            issues: [{ code: "metadata_timeout", message: "Contact metadata could not be acquired." }],
        } };
        expect(validateDriverCatalog(unavailable, "realm-a")).toBe(unavailable);
        expect(() => validateDriverCatalog({ ...unavailable, coverage: { ...unavailable.coverage, status: "partial" } }, "realm-a"))
            .toThrow("incomplete");
    });
});

describe("read-only GL-to-forecast association", () => {
    it("keeps GL-only/inactive names and leaves unrelated forecast rows separate", () => {
        const inactive = entry({ key: "vendor:300", name: "Inactive supplier", entity_type: "vendor", entity_id: "300", active: false });
        const forecast = row({ name: "Different forecast assumption" });
        const result = associateDriverCatalog(catalog([entry(), inactive]), [forecast], [], 1, 4);
        expect(result.entries).toHaveLength(2);
        expect(result.entries.map((item) => item.status)).toEqual(["not_in_forecast", "not_in_forecast"]);
        expect(result.entries[1].entry).toBe(inactive);
        expect(result.entries[1].entry.active).toBe(false);
        expect(result.unlinkedCustomers).toEqual([forecast]);
    });

    it("prefers a unique typed source ID despite a different display name or an alias collision", () => {
        const namedOther = entry({ key: "customer:201", entity_id: "201" });
        const forecast = row({ name: "Renamed forecast row", qbo_id: "200" });
        const result = associateDriverCatalog(catalog([entry({ aliases: ["Renamed forecast row"] }), namedOther]), [forecast], [], 1, 4);
        expect(result.entries[0]).toMatchObject({ row: forecast, rowType: "customer", matchBasis: "source_id", status: "scheduled" });
        expect(result.entries[1].row).toBeNull();
        expect(result.unlinkedCustomers).toEqual([]);
    });

    it("permits only a unique exact name or alias when no authoritative row ID is present", () => {
        const forecast = row({ name: "Known alias" });
        const result = associateDriverCatalog(catalog([entry({ aliases: ["Known alias"] })]), [forecast], [], 1, 4);
        expect(result.entries[0].row).toBe(forecast);
        expect(result.entries[0].matchBasis).toBe("name");
        expect(result.unlinkedCustomers).toEqual([]);
        const caseDifference = associateDriverCatalog(catalog(), [row({ name: "synthetic alder" })], [], 1, 4);
        expect(caseDifference.entries[0].status).toBe("not_in_forecast");
    });

    it("allows typed GL classification/name without a contact ID but holds name-inferred identities for review", () => {
        const forecast = row();
        const typed = associateDriverCatalog(catalog([entry({ entity_id: null, identity_basis: "typed_gl" })]), [forecast], [], 1, 4);
        expect(typed.entries[0]).toMatchObject({ row: forecast, matchBasis: "name", status: "scheduled" });
        for (const identity_basis of ["unique_name", "unresolved"] as const) {
            const result = associateDriverCatalog(catalog([entry({ identity_basis })]), [forecast], [], 1, 4);
            expect(result.entries[0]).toMatchObject({ row: null, matchBasis: null, status: "needs_review" });
            expect(result.unlinkedCustomers[0]).toBe(forecast);
        }
    });

    it("does not confuse local row/override keys or unproven entity IDs with accounting IDs", () => {
        const local = { ...row({ name: "Different name", row_key: "200" }), row_id: "200", entity_id: "200", entity_key: "200", id: "200" };
        const result = associateDriverCatalog(catalog(), [local], [], 1, 4);
        expect(result.entries[0]).toMatchObject({ row: null, matchBasis: null, status: "not_in_forecast" });
        expect(result.unlinkedCustomers[0]).toBe(local);
    });

    it("never automatically links manual assumptions even when name and source ID collide", () => {
        const manual = row({ source: "manual", qbo_id: "200" });
        const result = associateDriverCatalog(catalog(), [manual], [], 1, 4);
        expect(result.entries[0]).toMatchObject({ row: null, status: "not_in_forecast" });
        expect(result.unlinkedCustomers[0]).toBe(manual);
    });

    it("keeps distinct same-name GL identities and duplicate forecast names unmerged", () => {
        const forecast = row();
        const duplicatedContacts = associateDriverCatalog(catalog([entry(), entry({ key: "customer:201", entity_id: "201" })]), [forecast], [], 1, 4);
        expect(duplicatedContacts.entries.map((item) => item.status)).toEqual(["needs_review", "needs_review"]);
        expect(duplicatedContacts.entries.every((item) => item.row === null)).toBe(true);
        expect(duplicatedContacts.unlinkedCustomers).toEqual([forecast]);
        const second = row({ row_key: "forecast-b" });
        const duplicatedRows = associateDriverCatalog(catalog(), [forecast, second], [], 1, 4);
        expect(duplicatedRows.entries[0]).toMatchObject({ row: null, status: "needs_review" });
        expect(duplicatedRows.unlinkedCustomers).toEqual([forecast, second]);
    });

    it("does not name-link through an unresolved same-name GL identity", () => {
        const unknown = entry({ key: "unresolved:other", entity_type: "unresolved", entity_id: "other", identity_basis: "unresolved" });
        const result = associateDriverCatalog(catalog([entry(), unknown]), [row()], [], 1, 4);
        expect(result.entries.map((item) => item.status)).toEqual(["needs_review", "needs_review"]);
        const explicit = row({ qbo_id: "200" });
        const resolved = associateDriverCatalog(catalog([entry(), unknown]), [explicit], [], 1, 4);
        expect(resolved.entries[0]).toMatchObject({ row: explicit, matchBasis: "source_id", status: "scheduled" });
        expect(resolved.entries[1]).toMatchObject({ row: null, status: "needs_review" });
    });

    it("holds contradictory or multiply claimed accounting IDs for review", () => {
        const contrary = row({ qbo_id: "201" });
        expect(associateDriverCatalog(catalog(), [contrary], [], 1, 4).entries[0]).toMatchObject({ row: null, status: "needs_review" });
        const sameIdRows = [row({ qbo_id: "200" }), row({ row_key: "second", qbo_id: "200" })];
        expect(associateDriverCatalog(catalog(), sameIdRows, [], 1, 4).entries[0]).toMatchObject({ row: null, status: "needs_review" });
        const contacts = catalog([entry(), entry({ key: "customer:200:duplicate", name: "Other name" })]);
        expect(associateDriverCatalog(contacts, [row({ qbo_id: "200" })], [], 1, 4).entries.every((item) => item.status === "needs_review" && item.row === null)).toBe(true);
    });

    it("does not let a weaker name link claim a row with ambiguous strong ID claims", () => {
        const forecast = row({ name: "Unique forecast alias", qbo_id: "200" });
        const contacts = catalog([
            entry({ name: "First source name" }),
            entry({ key: "customer:200:duplicate", name: "Second source name" }),
            entry({ key: "customer:typed", name: "Third source name", entity_id: null, identity_basis: "typed_gl", aliases: ["Unique forecast alias"] }),
        ]);
        const result = associateDriverCatalog(contacts, [forecast], [], 1, 4);
        expect(result.entries.every((item) => item.status === "needs_review" && item.row === null)).toBe(true);
        expect(result.unlinkedCustomers).toEqual([forecast]);
    });

    it("keeps different-name forecast rows with a duplicate contact ID unavailable to weaker name links", () => {
        const first = row({ name: "First forecast name", qbo_id: "200" });
        const second = row({ row_key: "second", name: "Second forecast name", qbo_id: "200" });
        const identified = entry({ name: "GL contact name" });
        const nameOnly = entry({ key: "customer:typed", name: "First forecast name", entity_id: null, identity_basis: "typed_gl" });

        for (const contacts of [[identified, nameOnly], [nameOnly, identified]]) {
            const result = associateDriverCatalog(catalog(contacts), [first, second], [], 1, 4);
            expect(result.entries.every((item) => item.status === "needs_review" && item.row === null)).toBe(true);
            expect(result.unlinkedCustomers).toEqual([first, second]);
        }
    });

    it("keeps customer and vendor namespaces separate", () => {
        const customer = row({ qbo_id: 200 });
        const vendor = row({ row_key: "vendor-row", qbo_id: 200 });
        const data = catalog([entry(), entry({ key: "vendor:200", entity_type: "vendor" })]);
        const result = associateDriverCatalog(data, [customer], [vendor], 1, 4);
        expect(result.entries[0].row).toBe(customer);
        expect(result.entries[1].row).toBe(vendor);
    });

    it.each([1.5, Number.MAX_SAFE_INTEGER + 1, NaN, Infinity])("does not use the invalid numeric contact ID %s for association", (qbo_id) => {
        const forecast = row({ qbo_id });
        const result = associateDriverCatalog(catalog([entry({ entity_id: String(qbo_id) })]), [forecast], [], 1, 4);
        expect(result.entries[0]).toMatchObject({ row: null, matchBasis: null, status: "needs_review" });
        expect(result.unlinkedCustomers[0]).toBe(forecast);
    });

    it("derives current status only from future columns; null is unknown and zero is data", () => {
        const forecast = row({ amounts: [9999, 0, 0, 0] });
        expect(forecastDriverStatus(forecast, 1, 4)).toBe("no_future_cash");
        expect(associateDriverCatalog(catalog(), [forecast], [], 1, 4).entries[0].status).toBe("no_future_cash");
        forecast.amounts = [9999, 0, -50, 0];
        expect(forecastDriverStatus(forecast, 1, 4)).toBe("scheduled");
        forecast.amounts = [9999, 0, null, 0];
        const unknown = associateDriverCatalog(catalog(), [forecast], [], 1, 4).entries[0];
        expect(unknown).toMatchObject({ row: forecast, status: "needs_review" });
        forecast.amounts = [9999, 0];
        expect(forecastDriverStatus(forecast, 1, 4)).toBe("needs_review");
        expect(forecastDriverStatus(forecast, 1, 1)).toBe("needs_review");
    });

    it("does not mutate catalogs, frozen rows or arrays", () => {
        const forecast = row(); Object.freeze(forecast.amounts); Object.freeze(forecast);
        const data = catalog(); Object.freeze(data.entries[0].aliases); Object.freeze(data.entries[0]); Object.freeze(data.entries); Object.freeze(data.coverage.issues); Object.freeze(data.coverage); Object.freeze(data);
        const customers = [forecast]; Object.freeze(customers);
        const before = JSON.stringify({ data, customers });
        const result = associateDriverCatalog(data, customers, [], 1, 4);
        expect(result.entries[0].row).toBe(forecast);
        expect(JSON.stringify({ data, customers })).toBe(before);
    });
});
