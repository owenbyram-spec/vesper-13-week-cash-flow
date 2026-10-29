import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockStoreState } = vi.hoisted(() => ({
    mockStoreState: { activeCompanyId: "realm-a" as string | null },
}));

vi.mock("@/lib/store", () => ({
    useAppStore: (selector: (state: typeof mockStoreState) => unknown) => selector(mockStoreState),
}));
vi.mock("@/lib/api/client", () => ({
    apiClient: {
        get: vi.fn(),
        post: vi.fn(),
        delete: vi.fn(),
    },
}));
vi.mock("@/lib/api/cashflow-forecast-export", () => ({
    downloadCashflowForecastExport: vi.fn(),
}));
vi.mock("@/lib/api/cff-integrity", () => ({
    addForecastRow: vi.fn(),
}));
vi.mock("@/components/ui/modals", () => ({
    SaveStatus: ({ state }: { state: string }) => <span data-testid="save-state">{state}</span>,
}));
vi.mock("@/components/ui/qbo-not-connected", () => ({
    QboNotConnected: () => <div>Connect QuickBooks</div>,
}));
vi.mock("./unmapped-entities-banner", () => ({
    UnmappedEntitiesBanner: ({
        onAddRow,
    }: {
        onAddRow: (entity: {
            entity_name: string;
            entity_type: "vendor";
            transaction_count: number;
            total_amount: number;
            first_week_ending: string;
            last_week_ending: string;
        }) => Promise<void>;
    }) => (
        <button
            onClick={() => {
                void onAddRow({
                    entity_name: "Banner Vendor",
                    entity_type: "vendor",
                    transaction_count: 1,
                    total_amount: 25,
                    first_week_ending: "2026-07-31",
                    last_week_ending: "2026-07-31",
                }).catch(() => undefined);
            }}
        >
            Add banner row
        </button>
    ),
}));

import { apiClient } from "@/lib/api/client";
import { addForecastRow } from "@/lib/api/cff-integrity";
import { downloadCashflowForecastExport } from "@/lib/api/cashflow-forecast-export";
import CashFlowForecastPage from "./page";
import type { DriverCatalog, DriverCatalogEntry } from "@/lib/api/driver-catalog";

function deferred<T>() {
    let resolve!: (value: T | PromiseLike<T>) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function forecastResponse(
    companyId: string,
    exportId = `${companyId}-export-1`,
    overrides: Record<string, unknown> = {},
) {
    const companyLabel = companyId === "realm-a" ? "Acme" : "Beta";
    return {
        week_headers: ["2026-07-31", "2026-08-07"],
        lookback: 1,
        forecast_reanchored_to_control: false,
        beginning_balances: [1_000, 1_101.11],
        opening_balance_week_ending: "2026-07-31",
        first_forecast_week_ending: "2026-08-07",
        customers: [{
            name: `${companyLabel} Customer`,
            day_of_month: 15,
            effective_day: 15,
            avg_amount: 1_234.5,
            effective_avg: 1_234.5,
            amounts: [111.11, 0],
            sort_order: 0,
        }],
        other_cash_in: [0, 0],
        total_cash_in: [111.11, 0],
        vendors: [{
            name: `${companyLabel} Vendor`,
            day_of_month: 20,
            effective_day: 20,
            avg_amount: 25,
            effective_avg: 25,
            amounts: [10, 0],
            sort_order: 0,
        }],
        other_cash_out: [0, 0],
        total_cash_out: [10, 0],
        ending_balances: [1_101.11, 1_101.11],
        bank_balance: 1_000,
        bank_balance_as_of: "2026-07-24",
        beginning_balance_override: null,
        uncategorized_actuals: 0,
        week_statuses: [],
        review_required_weeks: [],
        data_quality: { is_partial: false, unavailable_sections: [] },
        unmatched: { count: 0, reasons: {}, entities: [] },
        beyond_horizon: null,
        snapshot_age_seconds: 0,
        snapshot_state: "ready",
        export_id: exportId,
        export_company_id: companyId,
        export_schema_version: 1,
        export_capture_state: "ready",
        export_error: null,
        ...overrides,
    };
}

function companyFromConfig(config?: unknown) {
    return (config as { params?: { company_id?: string } } | undefined)?.params?.company_id;
}

function glCatalogResponse(companyId: string, entries?: DriverCatalogEntry[]): DriverCatalog {
    const label = companyId === "realm-a" ? "Acme" : "Beta";
    const names = entries ?? [
        { key: "customer:200", name: `${label} Customer`, entity_type: "customer", entity_id: "200", aliases: [], active: true, first_seen: "2026-07-01", last_seen: "2026-07-30", gl_line_count: 1, identity_basis: "gl_id" },
        { key: "vendor:300", name: `${label} Vendor`, entity_type: "vendor", entity_id: "300", aliases: [], active: true, first_seen: "2026-07-01", last_seen: "2026-07-30", gl_line_count: 1, identity_basis: "gl_id" },
    ];
    return { schema_version: 1, source: "GeneralLedger", company_id: companyId, start_date: "2026-02-02", end_date: "2026-07-31", observed_at: "2026-08-01T12:00:00Z", coverage: { status: "complete", issues: [], data_rows: names.length, named_rows: names.length, unattributed_rows: 0 }, entries: names };
}

function seedReadyForecasts() {
    vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
        if (url === "/cashflow-forecast/weekly") {
            const companyId = companyFromConfig(config) || "realm-a";
            return Promise.resolve({ data: forecastResponse(companyId) });
        }
        if (url === "/cashflow-forecast/driver-catalog") {
            return Promise.resolve({ data: glCatalogResponse(companyFromConfig(config) || "realm-a") });
        }
        return Promise.resolve({ data: {} });
    });
    vi.mocked(apiClient.post).mockImplementation((url: string, _body?: unknown, config?: unknown) => {
        if (url === "/cashflow-forecast/weekly/refresh") {
            const companyId = companyFromConfig(config) || "realm-a";
            return Promise.resolve({ data: forecastResponse(companyId, `${companyId}-refresh`) });
        }
        return Promise.resolve({ data: {} });
    });
    vi.mocked(apiClient.delete).mockResolvedValue({ data: {} });
    vi.mocked(addForecastRow).mockResolvedValue({
        status: "row_created",
        entity_name: "Banner Vendor",
        entity_type: "vendor",
        override_id: 1,
        snapshot_refresh_enqueued: true,
    });
    vi.mocked(downloadCashflowForecastExport).mockResolvedValue("downloaded");
}

function editableMoneyCell(value: string) {
    const cell = screen.getAllByText(value).find((candidate) => (
        candidate.tagName === "TD" && candidate.className.includes("cursor-pointer")
    ));
    if (!cell) throw new Error(`No editable cell found for ${value}`);
    return cell;
}

function gridWeekCell(rowLabel: string, weekIndex: number) {
    const row = screen.getByText(rowLabel).closest("tr");
    if (!row) throw new Error(`No grid row found for ${rowLabel}`);
    return within(row).getAllByRole("cell")[weekIndex + 3];
}

function actualTransaction(overrides: Record<string, unknown> = {}) {
    return {
        id: 41,
        qbo_txn_id: "qbo-41",
        qbo_txn_type: "Deposit",
        qbo_account_id: "bank-1",
        txn_date: "2026-07-30",
        amount: 125.55,
        direction: "in",
        description: "Lifecycle receipt",
        account_name: "Operating Checking",
        entity_name: null,
        entity_type: null,
        category: "uncategorized",
        is_billcom: false,
        ...overrides,
    };
}

function actualsPullResponse(transactions: Array<Record<string, unknown>>) {
    return {
        transactions,
        week_ending: "2026-07-31",
        cash_account_universe: {
            included_count: 1,
            clearing_count: 0,
            unmatched_balance_sheet_row_count: 0,
            warnings: [],
        },
    };
}

describe("CashFlowForecastPage — exact export identity and operation barrier", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockStoreState.activeCompanyId = "realm-a";
        seedReadyForecasts();
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("names Vesper Bot in the cash-flow spread guidance", async () => {
        render(<CashFlowForecastPage />);
        expect(await screen.findByText(/Ask Vesper Bot something like/)).toBeInTheDocument();
    });

    it("opens driver details without editing, deleting or replacing the forecast", async () => {
        render(<CashFlowForecastPage />);
        await screen.findByText("Acme Customer");
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(screen.queryByText("Entity Reference")).not.toBeInTheDocument();
        const row = screen.getByText("Acme Customer").closest("tr")!;
        const before = Array.from(row.cells, (cell) => cell.textContent);
        const inspect = screen.getByRole("button", { name: "Inspect customer Acme Customer driver" });
        inspect.focus();
        fireEvent.click(inspect);
        const drawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(within(drawer).getByRole("heading", { name: "Acme Customer" })).toBeInTheDocument();
        expect(vi.mocked(apiClient.get).mock.calls.some(([url]) => url === "/cashflow-forecast/driver-catalog")).toBe(false);
        expect(within(drawer).queryByText("Acme Vendor")).not.toBeInTheDocument();
        expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
        fireEvent.keyDown(document, { key: "Escape" });
        await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
        expect(inspect).toHaveFocus();
        expect(Array.from(row.cells, (cell) => cell.textContent)).toEqual(before);
        expect(apiClient.post).not.toHaveBeenCalled();
        expect(apiClient.delete).not.toHaveBeenCalled();
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
    });

    it("clears an open driver directory when the workspace changes", async () => {
        const { rerender } = render(<CashFlowForecastPage />);
        await screen.findByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        const drawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(within(drawer).getByText("Acme Vendor")).toBeInTheDocument();
        mockStoreState.activeCompanyId = "realm-b";
        rerender(<CashFlowForecastPage />);
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
        await screen.findByText("Beta Customer");
        const trigger = screen.getByRole("button", { name: "Drivers" });
        expect(trigger).toHaveAttribute("aria-expanded", "false");
        fireEvent.click(trigger);
        const nextDrawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(within(nextDrawer).getByText("Beta Customer")).toBeInTheDocument();
        expect(within(nextDrawer).queryByText("Acme Customer")).not.toBeInTheDocument();
        expect(apiClient.post).not.toHaveBeenCalled();
    });

    it("inspects duplicate names by row identity instead of selecting the first match", async () => {
        vi.mocked(apiClient.get).mockResolvedValue({ data: forecastResponse("realm-a", "duplicates", {
            customers: [
                { row_id: "customer:dup-a", name: "Duplicate Customer", day_of_month: 10, effective_day: 10, avg_amount: 111.11, effective_avg: 111.11, amounts: [111.11, 0], sort_order: 0 },
                { row_id: "customer:dup-b", name: "Duplicate Customer", day_of_month: 20, effective_day: 20, avg_amount: 222.22, effective_avg: 222.22, amounts: [222.22, 0], sort_order: 1 },
            ],
        }) });
        render(<CashFlowForecastPage />);
        const second = await screen.findByTestId("forecast-row-customer:dup-b");
        fireEvent.click(within(second).getByRole("button", { name: "Inspect customer Duplicate Customer driver" }));
        const drawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(within(drawer).getByText("$222.22")).toBeInTheDocument();
        expect(within(drawer).queryByText("$111.11")).not.toBeInTheDocument();
        expect(apiClient.post).not.toHaveBeenCalled();
    });

    it("preserves the CFF feature census and renders money with exact zero/cents semantics", async () => {
        render(<CashFlowForecastPage />);
        expect(await screen.findAllByText("Acme Customer")).not.toHaveLength(0);

        for (const action of [
            "Download forecast (Excel)",
            "Download forecast (PDF)",
            "+ Add Spread",
            "📥 Pull Transactions",
            "Refresh from QBO",
            "+ Add Customer",
            "+ Add Vendor",
            "Add banner row",
        ]) {
            expect(screen.getByRole("button", { name: action })).toBeInTheDocument();
        }
        expect(screen.getAllByText("Avg/Mo").length).toBeGreaterThan(0);
        expect(screen.getAllByText("$1,234.50").length).toBeGreaterThan(0);
        expect(screen.getAllByText("$0.00").length).toBeGreaterThan(0);
        expect(screen.getByText("Non Routine Expenses")).toBeInTheDocument();
        expect(screen.getByText("Ending Bank Balance")).toBeInTheDocument();
    });

    it("preserves an intentional lookback re-anchor while recomputing cents on both sides", async () => {
        const firstSave = deferred<{ data: Record<string, never> }>();
        const secondSave = deferred<{ data: Record<string, never> }>();
        let weeklyReads = 0;
        let saveCount = 0;
        const anchoredForecast = (firstAmount: number, secondAmount: number, exportId: string) =>
            forecastResponse("realm-a", exportId, {
                forecast_reanchored_to_control: true,
                beginning_balances: [1_000.01, 1_500.55],
                customers: [{
                    name: "Acme Customer",
                    day_of_month: 15,
                    effective_day: 15,
                    avg_amount: 1_234.5,
                    effective_avg: 1_234.5,
                    amounts: [firstAmount, secondAmount],
                    sort_order: 0,
                }],
                vendors: [{
                    name: "Acme Vendor",
                    day_of_month: 20,
                    effective_day: 20,
                    avg_amount: 25,
                    effective_avg: 25,
                    amounts: [50, 25.01],
                    sort_order: 0,
                }],
                total_cash_in: [firstAmount, secondAmount],
                total_cash_out: [50, 25.01],
                ending_balances: [
                    Math.round((1_000.01 + firstAmount - 50) * 100) / 100,
                    Math.round((1_500.55 + secondAmount - 25.01) * 100) / 100,
                ],
            });
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            if (weeklyReads === 1) {
                return Promise.resolve({ data: anchoredForecast(100.01, 200.02, "anchor-initial") });
            }
            if (weeklyReads === 2) {
                return Promise.resolve({ data: anchoredForecast(101.02, 200.02, "anchor-after-history") });
            }
            return Promise.resolve({ data: anchoredForecast(101.02, 201.03, "anchor-after-forecast") });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/override") return Promise.resolve({ data: {} });
            saveCount += 1;
            return saveCount === 1 ? firstSave.promise : secondSave.promise;
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");

        fireEvent.mouseDown(editableMoneyCell("$100.01"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "101.02" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(saveCount).toBe(1));
        expect(gridWeekCell("Ending Bank Balance", 0)).toHaveTextContent("$1,051.03");
        expect(gridWeekCell("Beginning Bank Balance", 1)).toHaveTextContent("$1,500.55");
        expect(gridWeekCell("Ending Bank Balance", 1)).toHaveTextContent("$1,675.56");
        expect(weeklyReads).toBe(1);

        await act(async () => {
            firstSave.resolve({ data: {} });
            await firstSave.promise;
        });
        await waitFor(() => expect(weeklyReads).toBe(2));

        fireEvent.mouseDown(editableMoneyCell("$200.02"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "201.03" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(saveCount).toBe(2));
        expect(gridWeekCell("Beginning Bank Balance", 1)).toHaveTextContent("$1,500.55");
        expect(gridWeekCell("Ending Bank Balance", 1)).toHaveTextContent("$1,676.57");

        await act(async () => {
            secondSave.resolve({ data: {} });
            await secondSave.promise;
        });
    });

    it("keeps ordinary rolling continuity when control re-anchoring is off", async () => {
        const pendingSave = deferred<{ data: Record<string, never> }>();
        vi.mocked(apiClient.get).mockResolvedValue({
            data: forecastResponse("realm-a", "continuity-off", {
                forecast_reanchored_to_control: false,
                beginning_balances: [1_000.01, 1_500.55],
                customers: [{
                    name: "Acme Customer",
                    day_of_month: 15,
                    effective_day: 15,
                    avg_amount: 1_234.5,
                    effective_avg: 1_234.5,
                    amounts: [100.01, 200.02],
                    sort_order: 0,
                }],
                vendors: [{
                    name: "Acme Vendor",
                    day_of_month: 20,
                    effective_day: 20,
                    avg_amount: 25,
                    effective_avg: 25,
                    amounts: [50, 25.01],
                    sort_order: 0,
                }],
                total_cash_in: [100.01, 200.02],
                total_cash_out: [50, 25.01],
                ending_balances: [1_050.02, 1_675.56],
            }),
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/override" ? pendingSave.promise : Promise.resolve({ data: {} })
        ));

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.mouseDown(editableMoneyCell("$100.01"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "101.02" } });
        fireEvent.blur(screen.getByRole("spinbutton"));

        await waitFor(() => expect(gridWeekCell("Beginning Bank Balance", 1)).toHaveTextContent("$1,051.03"));
        expect(gridWeekCell("Ending Bank Balance", 1)).toHaveTextContent("$1,226.04");

        await act(async () => {
            pendingSave.resolve({ data: {} });
            await pendingSave.promise;
        });
    });

    it("keeps a newer exclusion when an older categorization fails late on the same actual", async () => {
        const olderCategorize = deferred<{ data: Record<string, never> }>();
        const newerExclude = deferred<{ data: Record<string, never> }>();
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url === "/cashflow-forecast/weekly") {
                weeklyReads += 1;
                return Promise.resolve({
                    data: forecastResponse(
                        "realm-a",
                        weeklyReads === 1 ? "actual-initial" : "actual-after-newer-success",
                    ),
                });
            }
            if (url === "/cashflow-forecast/actuals/pull") {
                return Promise.resolve({ data: actualsPullResponse([actualTransaction()]) });
            }
            if (url === "/cashflow-forecast/entities") {
                return Promise.resolve({ data: { customers: [], vendors: [] } });
            }
            return Promise.resolve({ data: {} });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string, body?: unknown) => {
            if (url !== "/cashflow-forecast/actuals/categorize") return Promise.resolve({ data: {} });
            const category = (body as { category?: string }).category;
            return category === "categorized" ? olderCategorize.promise : newerExclude.promise;
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "📥 Pull Transactions" }));
        const entityInput = await screen.findByPlaceholderText("Select customer...");
        fireEvent.focus(entityInput);
        fireEvent.mouseDown(screen.getByRole("button", { name: "Acme Customer" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/categorize",
            expect.objectContaining({ id: 41, category: "categorized" }),
        ));
        await waitFor(() => expect(screen.getByText("✓")).toBeInTheDocument());

        fireEvent.click(screen.getByTitle("Exclude"));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/categorize",
            expect.objectContaining({ id: 41, category: "excluded" }),
        ));
        await act(async () => {
            newerExclude.resolve({ data: {} });
            await newerExclude.promise;
        });
        await waitFor(() => expect(screen.getByTitle("Include")).toBeInTheDocument());

        await act(async () => {
            olderCategorize.reject(new Error("older categorization failed late"));
            await Promise.resolve();
        });
        expect(screen.getByTitle("Include")).toBeInTheDocument();
        expect(screen.getByText("SKIP")).toBeInTheDocument();
        expect(screen.queryByText("Couldn't categorize this transaction. Please try again.")).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({
                exportId: "actual-after-newer-success",
                expectedCompanyId: "realm-a",
            }),
        ));
        expect(weeklyReads).toBe(2);
    });

    it("registers pull, categorize, exclude, reset, and close before adopting the downloadable forecast", async () => {
        let weeklyReads = 0;
        let actualsPulls = 0;
        const categorizedReceipt = actualTransaction({
            entity_name: "Acme Customer",
            entity_type: "customer",
            category: "categorized",
        });
        const categorizedBill = actualTransaction({
            id: 42,
            qbo_txn_id: "qbo-42",
            qbo_txn_type: "BillPayment",
            amount: 75.25,
            direction: "out",
            description: "Lifecycle bill",
            entity_name: "Acme Vendor",
            entity_type: "vendor",
            category: "categorized",
        });
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url === "/cashflow-forecast/weekly") {
                weeklyReads += 1;
                return Promise.resolve({
                    data: forecastResponse(
                        "realm-a",
                        weeklyReads === 1 ? "lifecycle-initial" : "lifecycle-after-close",
                    ),
                });
            }
            if (url === "/cashflow-forecast/actuals/pull") {
                actualsPulls += 1;
                const transactions = actualsPulls === 1
                    ? [actualTransaction(), categorizedBill]
                    : [categorizedReceipt, { ...categorizedBill, category: "excluded" }];
                return Promise.resolve({ data: actualsPullResponse(transactions) });
            }
            if (url === "/cashflow-forecast/entities") {
                return Promise.resolve({ data: { customers: [], vendors: [] } });
            }
            return Promise.resolve({ data: {} });
        });
        vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
        vi.mocked(apiClient.delete).mockResolvedValue({ data: {} });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "📥 Pull Transactions" }));
        const receiptInput = await screen.findByPlaceholderText("Select customer...");
        fireEvent.focus(receiptInput);
        fireEvent.mouseDown(screen.getByRole("button", { name: "Acme Customer" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/categorize",
            expect.objectContaining({ id: 41, category: "categorized" }),
        ));

        const billRow = screen.getByText("Lifecycle bill").closest("tr");
        if (!billRow) throw new Error("Lifecycle bill row not found");
        fireEvent.click(within(billRow).getByTitle("Exclude"));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/categorize",
            expect.objectContaining({ id: 42, category: "excluded" }),
        ));

        fireEvent.click(screen.getByTitle("Re-pull from QBO"));
        await waitFor(() => expect(apiClient.delete).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/reset",
            expect.objectContaining({ params: expect.objectContaining({ week_ending: "2026-07-31" }) }),
        ));
        await waitFor(() => expect(actualsPulls).toBe(2));

        fireEvent.click(screen.getByRole("button", { name: "Done — Update Forecast" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/actuals/close",
            null,
            expect.objectContaining({
                params: expect.objectContaining({ company_id: "realm-a", week_ending: "2026-07-31" }),
            }),
        ));
        await waitFor(() => expect(screen.queryByText("Categorize Bank Transactions")).not.toBeInTheDocument());

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (PDF)" }));
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({
                exportId: "lifecycle-after-close",
                expectedCompanyId: "realm-a",
                format: "pdf",
            }),
        ));
        expect(actualsPulls).toBe(2);
        expect(weeklyReads).toBe(2);
    });

    it("linearizes a same-tick mutation before download and uses only the post-mutation ID", async () => {
        const pendingRemove = deferred<{ data: Record<string, never> }>();
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse(
                    "realm-a",
                    weeklyReads === 1 ? "same-tick-before" : "same-tick-after",
                ),
            });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string, body?: unknown) => {
            const request = body as { week_ending?: string } | undefined;
            if (url === "/cashflow-forecast/override" && request?.week_ending === "9997-12-31") {
                return pendingRemove.promise;
            }
            return Promise.resolve({ data: {} });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        act(() => {
            screen.getAllByTitle("Remove")[0].click();
            screen.getByRole("button", { name: "Download forecast (Excel)" }).click();
        });
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/override",
            expect.objectContaining({ week_ending: "9997-12-31" }),
        ));
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();

        await act(async () => {
            pendingRemove.resolve({ data: {} });
            await pendingRemove.promise;
        });
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({ exportId: "same-tick-after", expectedCompanyId: "realm-a" }),
        ));
        expect(weeklyReads).toBe(2);
    });

    it("rolls back a partially failed reorder and blocks export of the ambiguous server order", async () => {
        const customers = [{
            row_id: "customer:first",
            entity_key: "first",
            name: "First Customer",
            day_of_month: 10,
            effective_day: 10,
            avg_amount: 100,
            effective_avg: 100,
            amounts: [100, 0],
            sort_order: 0,
        }, {
            row_id: "customer:second",
            entity_key: "second",
            name: "Second Customer",
            day_of_month: 20,
            effective_day: 20,
            avg_amount: 200,
            effective_avg: 200,
            amounts: [200, 0],
            sort_order: 1,
        }];
        vi.mocked(apiClient.get).mockResolvedValue({
            data: forecastResponse("realm-a", "partial-order", {
                customers,
                total_cash_in: [300, 0],
                ending_balances: [1_290, 1_290],
            }),
        });
        vi.mocked(apiClient.post).mockImplementation((url: string, body?: unknown) => {
            const request = body as { entity_name?: string; week_ending?: string } | undefined;
            if (url === "/cashflow-forecast/override" && request?.week_ending === "9996-12-31") {
                return request.entity_name === "First Customer"
                    ? Promise.reject(new Error("one order write failed"))
                    : Promise.resolve({ data: {} });
            }
            return Promise.resolve({ data: {} });
        });

        render(<CashFlowForecastPage />);
        const first = await screen.findByTestId("forecast-row-customer:first");
        const second = screen.getByTestId("forecast-row-customer:second");
        fireEvent.dragStart(second);
        fireEvent.dragOver(first);
        fireEvent.drop(first);

        expect(await screen.findByText("one order write failed")).toBeInTheDocument();
        const ordered = document.querySelectorAll('[data-testid^="forecast-row-customer:"]');
        expect(ordered[0]).toHaveAttribute("data-testid", "forecast-row-customer:first");
        expect(ordered[1]).toHaveAttribute("data-testid", "forecast-row-customer:second");

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        expect(await screen.findByTestId("cff-export-error")).toHaveTextContent("A forecast change failed");
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
    });

    it("keeps actuals review open and blocks export when close adoption fails", async () => {
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url === "/cashflow-forecast/weekly") {
                weeklyReads += 1;
                return weeklyReads === 1
                    ? Promise.resolve({ data: forecastResponse("realm-a", "before-close-failure") })
                    : Promise.reject(new Error("close adoption failed"));
            }
            if (url === "/cashflow-forecast/actuals/pull") {
                return Promise.resolve({ data: actualsPullResponse([]) });
            }
            if (url === "/cashflow-forecast/entities") {
                return Promise.resolve({ data: { customers: [], vendors: [] } });
            }
            return Promise.resolve({ data: {} });
        });
        vi.mocked(apiClient.post).mockResolvedValue({ data: {} });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "📥 Pull Transactions" }));
        await screen.findByText("Categorize Bank Transactions");
        fireEvent.click(screen.getByRole("button", { name: "Done — Update Forecast" }));

        expect(await screen.findByText("close adoption failed")).toBeInTheDocument();
        expect(screen.getByText("Categorize Bank Transactions")).toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (PDF)" }));
        expect(await screen.findByTestId("cff-export-error")).toHaveTextContent("A forecast change failed");
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
        expect(weeklyReads).toBe(2);
    });

    it("discards a dirty refresh result and downloads only the later authoritative adoption", async () => {
        const pendingRefresh = deferred<{ data: ReturnType<typeof forecastResponse> }>();
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse(
                    "realm-a",
                    weeklyReads === 1 ? "dirty-before" : "dirty-authoritative",
                ),
            });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => {
            if (url === "/cashflow-forecast/weekly/refresh") return pendingRefresh.promise;
            return Promise.resolve({ data: {} });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Refresh from QBO" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/weekly/refresh",
            null,
            expect.anything(),
        ));
        fireEvent.click(screen.getAllByTitle("Remove")[0]);
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/override",
            expect.objectContaining({ week_ending: "9997-12-31" }),
        ));
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();

        await act(async () => {
            pendingRefresh.resolve({ data: forecastResponse("realm-a", "dirty-stale-refresh") });
            await pendingRefresh.promise;
        });
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({ exportId: "dirty-authoritative", expectedCompanyId: "realm-a" }),
        ));
        expect(downloadCashflowForecastExport).not.toHaveBeenCalledWith(
            expect.objectContaining({ exportId: "dirty-stale-refresh" }),
        );
        expect(weeklyReads).toBe(2);
    });

    it("ignores a late page mutation completion from the old company", async () => {
        const pendingASave = deferred<{ data: Record<string, never> }>();
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            const companyId = companyFromConfig(config) || "realm-a";
            return Promise.resolve({ data: forecastResponse(companyId) });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/override" ? pendingASave.promise : Promise.resolve({ data: {} })
        ));

        const view = render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.mouseDown(editableMoneyCell("$111.11"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "222.22" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/override",
            expect.objectContaining({ company_id: "realm-a", override_amount: 222.22 }),
        ));

        mockStoreState.activeCompanyId = "realm-b";
        view.rerender(<CashFlowForecastPage />);
        expect(await screen.findAllByText("Beta Customer")).not.toHaveLength(0);
        await act(async () => {
            pendingASave.resolve({ data: {} });
            await pendingASave.promise;
        });
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
        expect(screen.getAllByText("Beta Customer").length).toBeGreaterThan(0);
        const weeklyReads = vi.mocked(apiClient.get).mock.calls.filter(([url]) => (
            url === "/cashflow-forecast/weekly"
        ));
        expect(weeklyReads).toHaveLength(2);
        expect(companyFromConfig(weeklyReads[0][1])).toBe("realm-a");
        expect(companyFromConfig(weeklyReads[1][1])).toBe("realm-b");
    });

    it("revokes page-level download ownership when the company changes during blob work", async () => {
        const pendingDownload = deferred<"discarded">();
        let ownerCheck: (() => boolean) | undefined;
        vi.mocked(downloadCashflowForecastExport).mockImplementation((options) => {
            ownerCheck = options.isCurrentOwner;
            return pendingDownload.promise;
        });
        const view = render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        await waitFor(() => expect(ownerCheck).toBeDefined());
        expect(ownerCheck?.()).toBe(true);

        mockStoreState.activeCompanyId = "realm-b";
        view.rerender(<CashFlowForecastPage />);
        expect(await screen.findAllByText("Beta Customer")).not.toHaveLength(0);
        expect(ownerCheck?.()).toBe(false);

        await act(async () => {
            pendingDownload.resolve("discarded");
            await pendingDownload.promise;
        });
        expect(downloadCashflowForecastExport).toHaveBeenCalledOnce();
        expect(screen.queryByTestId("cff-export-error")).not.toBeInTheDocument();
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
    });

    it("removes company A before paint on switch and ignores its late refresh callbacks", async () => {
        const lateARefresh = deferred<{ data: ReturnType<typeof forecastResponse> }>();
        const pendingB = deferred<{ data: ReturnType<typeof forecastResponse> }>();
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            return companyFromConfig(config) === "realm-b"
                ? pendingB.promise
                : Promise.resolve({ data: forecastResponse("realm-a") });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/weekly/refresh"
                ? lateARefresh.promise
                : Promise.resolve({ data: {} })
        ));

        const view = render(<CashFlowForecastPage />);
        expect(await screen.findAllByText("Acme Customer")).not.toHaveLength(0);
        fireEvent.click(screen.getByRole("button", { name: "Refresh from QBO" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/weekly/refresh",
            null,
            expect.objectContaining({ params: expect.objectContaining({ company_id: "realm-a" }) }),
        ));

        mockStoreState.activeCompanyId = "realm-b";
        view.rerender(<CashFlowForecastPage />);
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
        expect(screen.getByText("Loading forecast…")).toBeInTheDocument();

        await act(async () => {
            lateARefresh.resolve({ data: forecastResponse("realm-a", "late-a") });
            await lateARefresh.promise;
        });
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
        expect(screen.queryAllByText("Beta Customer")).toHaveLength(0);

        await act(async () => {
            pendingB.resolve({ data: forecastResponse("realm-b") });
            await pendingB.promise;
        });
        expect(await screen.findAllByText("Beta Customer")).not.toHaveLength(0);
    });

    it("does not launch queued A mutation or refresh work after a same-turn company switch", async () => {
        const view = render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        vi.mocked(apiClient.post).mockClear();
        vi.mocked(addForecastRow).mockClear();

        act(() => {
            screen.getByRole("button", { name: "Refresh from QBO" }).click();
            screen.getByRole("button", { name: "Add banner row" }).click();
            mockStoreState.activeCompanyId = "realm-b";
            view.rerender(<CashFlowForecastPage />);
        });
        await act(async () => {
            await Promise.resolve();
            await Promise.resolve();
        });

        expect(apiClient.post).not.toHaveBeenCalled();
        expect(addForecastRow).not.toHaveBeenCalled();
        expect(screen.queryAllByText("Acme Customer")).toHaveLength(0);
    });

    it("downloads clean visible A during refresh B and coalesces same/cross-format clicks", async () => {
        const pendingRefresh = deferred<{ data: ReturnType<typeof forecastResponse> }>();
        const pendingDownload = deferred<"downloaded">();
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/weekly/refresh"
                ? pendingRefresh.promise
                : Promise.resolve({ data: {} })
        ));
        vi.mocked(downloadCashflowForecastExport).mockImplementation(() => pendingDownload.promise);

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Refresh from QBO" }));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalled());

        act(() => {
            fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
            fireEvent.click(screen.getByRole("button", { name: "Download forecast (PDF)" }));
        });

        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledOnce());
        expect(downloadCashflowForecastExport).toHaveBeenCalledWith(expect.objectContaining({
            exportId: "realm-a-export-1",
            expectedCompanyId: "realm-a",
            format: "excel",
        }));
        expect(vi.mocked(apiClient.get).mock.calls.filter(([url]) => (
            url === "/cashflow-forecast/weekly"
        ))).toHaveLength(1);

        await act(async () => {
            pendingDownload.resolve("downloaded");
            pendingRefresh.resolve({ data: forecastResponse("realm-a", "realm-a-refresh") });
            await Promise.all([pendingDownload.promise, pendingRefresh.promise]);
        });
    });

    it("commits an open edit, drains it, revalidates once, and downloads only the adopted ID", async () => {
        const pendingSave = deferred<{ data: Record<string, never> }>();
        const pendingRevalidation = deferred<{ data: ReturnType<typeof forecastResponse> }>();
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return weeklyReads === 1
                ? Promise.resolve({ data: forecastResponse("realm-a") })
                : pendingRevalidation.promise;
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/override"
                ? pendingSave.promise
                : Promise.resolve({ data: {} })
        ));

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.mouseDown(editableMoneyCell("$111.11"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "222.22" } });
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));

        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/override",
            expect.objectContaining({ company_id: "realm-a", override_amount: 222.22 }),
        ));
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();

        await act(async () => {
            pendingSave.resolve({ data: {} });
            await pendingSave.promise;
        });
        await waitFor(() => expect(weeklyReads).toBe(2));
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();

        await act(async () => {
            pendingRevalidation.resolve({
                data: forecastResponse("realm-a", "realm-a-export-after-save"),
            });
            await pendingRevalidation.promise;
        });
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({
                exportId: "realm-a-export-after-save",
                expectedCompanyId: "realm-a",
            }),
        ));
        expect(weeklyReads).toBe(2);
    });

    it("latches a failed mutation and blocks both the first download and retry", async () => {
        vi.mocked(apiClient.post).mockImplementation((url: string) => (
            url === "/cashflow-forecast/override"
                ? Promise.reject(new Error("save exploded"))
                : Promise.resolve({ data: {} })
        ));

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.mouseDown(editableMoneyCell("$111.11"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "333.33" } });
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (PDF)" }));

        expect(await screen.findByTestId("cff-export-error")).toHaveTextContent(
            "A forecast change failed",
        );
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
        const readsAfterFailure = vi.mocked(apiClient.get).mock.calls.length;

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        await waitFor(() => expect(screen.getByTestId("cff-export-error")).toBeInTheDocument());
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
        expect(vi.mocked(apiClient.get).mock.calls).toHaveLength(readsAfterFailure);
    });

    it("permits authoritative adoption after the same failed remove is corrected", async () => {
        let removeAttempts = 0;
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse(
                    "realm-a",
                    weeklyReads === 1 ? "realm-a-export-1" : "realm-a-after-correction",
                ),
            });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string, body?: unknown) => {
            const request = body as { week_ending?: string } | undefined;
            if (url === "/cashflow-forecast/override" && request?.week_ending === "9997-12-31") {
                removeAttempts += 1;
                return removeAttempts === 1
                    ? Promise.reject(new Error("remove failed"))
                    : Promise.resolve({ data: {} });
            }
            return Promise.resolve({ data: {} });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getAllByTitle("Remove")[0]);
        await waitFor(() => expect(removeAttempts).toBe(1));
        await waitFor(() => expect(screen.getAllByText("Acme Customer").length).toBeGreaterThan(0));

        fireEvent.click(screen.getAllByTitle("Remove")[0]);
        await waitFor(() => expect(removeAttempts).toBe(2));
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));

        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({
                exportId: "realm-a-after-correction",
                expectedCompanyId: "realm-a",
            }),
        ));
        expect(weeklyReads).toBe(2);
    });

    it("orders same-key correction by mutation epoch when the newer save settles first", async () => {
        const olderSave = deferred<{ data: Record<string, never> }>();
        const newerSave = deferred<{ data: Record<string, never> }>();
        let saveCount = 0;
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse("realm-a", "realm-a-after-newer-save", {
                    customers: [{
                        name: "Acme Customer",
                        day_of_month: 15,
                        effective_day: 15,
                        avg_amount: 1_234.5,
                        effective_avg: 1_234.5,
                        amounts: [333.33, 0],
                        sort_order: 0,
                    }],
                    total_cash_in: [333.33, 0],
                    ending_balances: [1_323.33, 1_323.33],
                }),
            });
        });
        vi.mocked(apiClient.post).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/override") return Promise.resolve({ data: {} });
            saveCount += 1;
            return saveCount === 1 ? olderSave.promise : newerSave.promise;
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");

        fireEvent.mouseDown(editableMoneyCell("$333.33"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "222.22" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(saveCount).toBe(1));

        fireEvent.mouseDown(editableMoneyCell("$222.22"));
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "333.33" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(saveCount).toBe(2));

        await act(async () => {
            newerSave.resolve({ data: {} });
            await newerSave.promise;
        });
        await waitFor(() => expect(weeklyReads).toBe(2));

        await act(async () => {
            olderSave.reject(new Error("older save failed late"));
            await Promise.resolve();
        });

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({
                exportId: "realm-a-after-newer-save",
                expectedCompanyId: "realm-a",
            }),
        ));
        expect(weeklyReads).toBe(3);
    });

    it("keeps explicit nulls distinct from zero through render and blank navigation", async () => {
        vi.mocked(apiClient.get).mockResolvedValue({
            data: forecastResponse("realm-a", "realm-a-null-grid", {
                beginning_balances: [1_000, null],
                customers: [{
                    name: "Nullable Customer",
                    day_of_month: null,
                    effective_day: null,
                    avg_amount: null,
                    effective_avg: null,
                    amounts: [null, 0],
                    sort_order: 0,
                    source: "invoice",
                    total_open_balance: null,
                }],
                total_cash_in: [null, 0],
                ending_balances: [null, null],
            }),
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Nullable Customer");
        const row = screen.getByTestId("forecast-row-customer:name:Nullable%20Customer:occurrence:0");
        const cells = row.querySelectorAll("td");
        const nullWeekCell = cells[3];
        const zeroWeekCell = cells[4];
        expect(cells[1]).toHaveTextContent("—");
        expect(cells[2]).toHaveTextContent("—");
        expect(nullWeekCell).toHaveTextContent("—");
        expect(zeroWeekCell).toHaveTextContent("$0.00");

        fireEvent.mouseDown(nullWeekCell);
        expect(screen.getByRole("spinbutton")).toHaveValue(null);
        fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Tab" });
        await waitFor(() => expect(row.querySelectorAll("td")[3]).toHaveTextContent("—"));
        expect(vi.mocked(apiClient.post).mock.calls.filter(([url]) => (
            url === "/cashflow-forecast/override"
        ))).toHaveLength(0);
        fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Escape" });
    });

    it("preserves duplicate-label rows as independently interactive local identities", async () => {
        const duplicateRows = (firstAmount: number) => [{
            row_id: "customer:dup-a",
            entity_key: "dup-a",
            name: "Duplicate Customer",
            day_of_month: 10,
            effective_day: 10,
            avg_amount: firstAmount,
            effective_avg: firstAmount,
            amounts: [firstAmount, 0],
            sort_order: 0,
        }, {
            row_id: "customer:dup-b",
            entity_key: "dup-b",
            name: "Duplicate Customer",
            day_of_month: 20,
            effective_day: 20,
            avg_amount: 222.22,
            effective_avg: 222.22,
            amounts: [222.22, 0],
            sort_order: 1,
        }];
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse("realm-a", "realm-a-duplicates", {
                    customers: duplicateRows(weeklyReads === 1 ? 111.11 : 150),
                    total_cash_in: [weeklyReads === 1 ? 333.33 : 372.22, 0],
                }),
            });
        });

        render(<CashFlowForecastPage />);
        const first = await screen.findByTestId("forecast-row-customer:dup-a");
        const second = screen.getByTestId("forecast-row-customer:dup-b");
        expect(first).toHaveAttribute("draggable", "true");
        expect(second).toHaveAttribute("draggable", "true");
        expect(within(first).getByTitle("Remove")).toBeInTheDocument();
        expect(within(second).getByTitle("Remove")).toBeInTheDocument();

        fireEvent.mouseDown(within(first).getAllByText("$111.11")[1]);
        fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "150.00" } });
        fireEvent.blur(screen.getByRole("spinbutton"));
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
            "/cashflow-forecast/override",
            expect.objectContaining({ entity_name: "Duplicate Customer", override_amount: 150 }),
        ));
        await waitFor(() => expect(within(screen.getByTestId("forecast-row-customer:dup-a")).getAllByText("$150.00")).toHaveLength(2));
        expect(within(screen.getByTestId("forecast-row-customer:dup-b")).getAllByText("$222.22")).toHaveLength(2);

        const firstAfterEdit = screen.getByTestId("forecast-row-customer:dup-a");
        const secondAfterEdit = screen.getByTestId("forecast-row-customer:dup-b");
        fireEvent.dragStart(secondAfterEdit);
        fireEvent.dragOver(firstAfterEdit);
        fireEvent.drop(firstAfterEdit);
        await waitFor(() => {
            const ordered = document.querySelectorAll('[data-testid^="forecast-row-customer:dup-"]');
            expect(ordered[0]).toHaveAttribute("data-testid", "forecast-row-customer:dup-b");
            expect(ordered[1]).toHaveAttribute("data-testid", "forecast-row-customer:dup-a");
        });

        fireEvent.click(within(screen.getByTestId("forecast-row-customer:dup-b")).getByTitle("Remove"));
        await waitFor(() => expect(screen.queryByTestId("forecast-row-customer:dup-b")).not.toBeInTheDocument());
        expect(screen.getByTestId("forecast-row-customer:dup-a")).toBeInTheDocument();
    });

    it("registers the child add through adoption before a later exact-ID download", async () => {
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: forecastResponse(
                    "realm-a",
                    weeklyReads === 1 ? "realm-a-export-1" : "realm-a-export-after-add",
                ),
            });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Add banner row" }));
        await waitFor(() => expect(addForecastRow).toHaveBeenCalledWith(
            "realm-a",
            "Banner Vendor",
            "vendor",
        ));
        await waitFor(() => expect(weeklyReads).toBe(2));

        fireEvent.click(screen.getByRole("button", { name: "Download forecast (PDF)" }));
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(
            expect.objectContaining({ exportId: "realm-a-export-after-add", format: "pdf" }),
        ));
    });

    it("surfaces building/unavailable capture honestly and never emits a file request", async () => {
        let weeklyReads = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            weeklyReads += 1;
            return Promise.resolve({
                data: weeklyReads === 1
                    ? forecastResponse("realm-a", "", {
                        snapshot_state: "building",
                        export_id: null,
                        export_capture_state: "building",
                    })
                    : forecastResponse("realm-a", "", {
                        export_id: null,
                        export_capture_state: "unavailable",
                        export_error: { user_message: "Capture failed safely", retryable: true },
                    }),
            });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        expect(await screen.findByTestId("cff-export-error")).toHaveTextContent("Capture failed safely");
        expect(downloadCashflowForecastExport).not.toHaveBeenCalled();
        expect(weeklyReads).toBe(2);
    });
});

describe("CashFlowForecastPage — history payees not projected as recurring", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockStoreState.activeCompanyId = "realm-a";
        seedReadyForecasts();
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("lists the payees the forecast did not project so the reviewer can add them", async () => {
        vi.mocked(apiClient.get).mockImplementation((url: string) => {
            if (url !== "/cashflow-forecast/weekly") return Promise.resolve({ data: {} });
            return Promise.resolve({
                data: forecastResponse("realm-a", "realm-a-export-1", {
                    non_recurring_history: [
                        {
                            name: "Insurer",
                            entity_type: "vendor",
                            occurrences: 1,
                            distinct_months: 1,
                            total_amount: 12_000,
                            last_date: "2026-07-20",
                            reason: "single_occurrence",
                        },
                        {
                            name: "Asset Buyer",
                            entity_type: "customer",
                            occurrences: 1,
                            distinct_months: 1,
                            total_amount: 250_000,
                            last_date: "2026-05-02",
                            reason: "single_occurrence",
                        },
                    ],
                }),
            });
        });

        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");

        const banner = screen.getByText(/not projected as recurring/);
        expect(banner).toHaveTextContent("2 payees from recent history are not projected as recurring");
        expect(banner).toHaveTextContent("Insurer ($12,000.00), Asset Buyer ($250,000.00)");
    });

    it("shows nothing when every history payee was projected or the snapshot predates the list", async () => {
        render(<CashFlowForecastPage />);
        await screen.findAllByText("Acme Customer");

        expect(screen.queryByText(/not projected as recurring/)).not.toBeInTheDocument();
    });
});

describe("CashFlowForecastPage — scoped GL driver directory", () => {
    beforeEach(() => { vi.clearAllMocks(); mockStoreState.activeCompanyId = "realm-a"; seedReadyForecasts(); });
    afterEach(() => { vi.restoreAllMocks(); });

    function catalogWithName(company: string, name: string) {
        return glCatalogResponse(company, [{ ...glCatalogResponse(company).entries[0], key: "customer:extra", entity_id: "extra", name }]);
    }

    it("loads GL names lazily, includes inactive GL-only contacts, and does not change financial/export state", async () => {
        const ledger = catalogWithName("realm-a", "Inactive GL-only customer");
        ledger.entries[0].active = false;
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            if (url === "/cashflow-forecast/weekly") return Promise.resolve({ data: forecastResponse(companyFromConfig(config) || "realm-a") });
            if (url === "/cashflow-forecast/driver-catalog") return Promise.resolve({ data: ledger });
            if (url === "/cashflow-forecast/entities") return Promise.resolve({ data: { customers: [{ name: "Unused master contact" }], vendors: [] } });
            return Promise.resolve({ data: {} });
        });
        render(<CashFlowForecastPage />);
        const customer = (await screen.findByText("Acme Customer")).closest("tr")!;
        const original = customer.textContent;
        expect(vi.mocked(apiClient.get).mock.calls.some(([url]) => url === "/cashflow-forecast/driver-catalog")).toBe(false);
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        const drawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(await within(drawer).findByText("Inactive GL-only customer")).toBeInTheDocument();
        expect(within(drawer).getByText("Inactive")).toBeInTheDocument();
        expect(within(drawer).getByText("Not in forecast")).toBeInTheDocument();
        expect(within(drawer).queryByText("Unused master contact")).not.toBeInTheDocument();
        fireEvent.click(within(drawer).getByRole("button", { name: "Close drivers" }));
        expect(customer.textContent).toBe(original);
        expect(screen.queryByText("Inactive GL-only customer")).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: "Download forecast (Excel)" }));
        await waitFor(() => expect(downloadCashflowForecastExport).toHaveBeenCalledWith(expect.objectContaining({ exportId: "realm-a-export-1" })));
        expect(apiClient.post).not.toHaveBeenCalled();
        expect(apiClient.delete).not.toHaveBeenCalled();
        expect(vi.mocked(apiClient.get).mock.calls.some(([url]) => url === "/cashflow-forecast/entities")).toBe(false);
    });

    it("never adopts a late directory from an older owner generation, including A to B to A", async () => {
        const oldA = deferred<{ data: DriverCatalog }>();
        const newA = deferred<{ data: DriverCatalog }>();
        let aRequests = 0;
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            const company = companyFromConfig(config) || "realm-a";
            if (url === "/cashflow-forecast/weekly") return Promise.resolve({ data: forecastResponse(company) });
            if (url === "/cashflow-forecast/driver-catalog") {
                if (company === "realm-a") return ++aRequests === 1 ? oldA.promise : newA.promise;
                return Promise.resolve({ data: catalogWithName(company, "Only B GL contact") });
            }
            return Promise.resolve({ data: {} });
        });
        const { rerender } = render(<CashFlowForecastPage />);
        await screen.findByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        expect(await screen.findByText("Loading GL directory…")).toBeInTheDocument();
        mockStoreState.activeCompanyId = "realm-b";
        rerender(<CashFlowForecastPage />);
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        await screen.findByText("Beta Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        expect(await screen.findByText("Only B GL contact")).toBeInTheDocument();
        mockStoreState.activeCompanyId = "realm-a";
        rerender(<CashFlowForecastPage />);
        expect(screen.queryByText("Only B GL contact")).not.toBeInTheDocument();
        await screen.findByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        await screen.findByText("Loading GL directory…");
        await act(async () => oldA.resolve({ data: catalogWithName("realm-a", "Old A GL contact") }));
        expect(screen.queryByText("Old A GL contact")).not.toBeInTheDocument();
        expect(screen.getByText("Loading GL directory…")).toBeInTheDocument();
        await act(async () => newA.resolve({ data: catalogWithName("realm-a", "Current A GL contact") }));
        expect(await screen.findByText("Current A GL contact")).toBeInTheDocument();
        expect(aRequests).toBe(2);
        expect(screen.queryByText("Only B GL contact")).not.toBeInTheDocument();
        expect(apiClient.post).not.toHaveBeenCalled();
    });

    it("retains a same-owner successful directory after transport and partial refresh failures", async () => {
        let catalogReads = 0;
        const retained = catalogWithName("realm-a", "Retained GL contact");
        const partial = catalogWithName("realm-a", "Partial replacement contact");
        partial.coverage.status = "partial";
        partial.coverage.issues = [{ code: "missing_page", message: "Some GL pages were unavailable" }];
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            if (url === "/cashflow-forecast/weekly") return Promise.resolve({ data: forecastResponse(companyFromConfig(config) || "realm-a") });
            if (url === "/cashflow-forecast/driver-catalog") {
                catalogReads += 1;
                if (catalogReads === 1) return Promise.resolve({ data: retained });
                if (catalogReads === 2) return Promise.reject(new Error("GL transport failed"));
                return Promise.resolve({ data: partial });
            }
            return Promise.resolve({ data: {} });
        });
        render(<CashFlowForecastPage />);
        await screen.findByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        await screen.findByText("Retained GL contact");
        fireEvent.click(screen.getByRole("button", { name: "Refresh GL directory" }));
        expect(await screen.findByRole("alert")).toHaveTextContent("Showing the retained directory");
        expect(screen.getByText("Retained GL contact")).toBeInTheDocument();
        expect(screen.getByText("Retained GL read")).toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: "Refresh GL directory" }));
        await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Some GL pages were unavailable"));
        expect(screen.getByText("Retained GL contact")).toBeInTheDocument();
        expect(screen.queryByText("Partial replacement contact")).not.toBeInTheDocument();
        expect(catalogReads).toBe(3);
        expect(apiClient.post).not.toHaveBeenCalled();
        expect(apiClient.delete).not.toHaveBeenCalled();
    });

    it("shows failed initial GL acquisition as unavailable rather than a complete projected-row directory", async () => {
        vi.mocked(apiClient.get).mockImplementation((url: string, config?: unknown) => {
            if (url === "/cashflow-forecast/weekly") return Promise.resolve({ data: forecastResponse(companyFromConfig(config) || "realm-a") });
            if (url === "/cashflow-forecast/driver-catalog") return Promise.reject(new Error("No source"));
            return Promise.resolve({ data: {} });
        });
        render(<CashFlowForecastPage />);
        await screen.findByText("Acme Customer");
        fireEvent.click(screen.getByRole("button", { name: "Drivers" }));
        const drawer = await screen.findByRole("dialog", { name: "Drivers" });
        expect(await within(drawer).findByRole("alert")).toHaveTextContent("GL directory unavailable");
        expect(within(drawer).queryByText("Complete read")).not.toBeInTheDocument();
        expect(within(drawer).queryByRole("region", { name: "Customers" })).not.toBeInTheDocument();
        expect(within(drawer).getByRole("region", { name: "Other forecast rows" })).toHaveTextContent("Acme Customer");
    });
});
