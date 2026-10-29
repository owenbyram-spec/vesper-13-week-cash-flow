import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DriversDrawer, type DriverEntity, type DriverSelection } from "./DriversDrawer";
import type { DriverCatalog, DriverCatalogEntry } from "@/lib/api/driver-catalog";

const weeks = ["2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13", "2026-11-20"];
const longName = "Synthetic Alder Consulting — full customer name across a long directory entry";

function entity(changes: Partial<DriverEntity> = {}): DriverEntity {
    return {
        row_key: "row-a", name: longName, day_of_month: 15, effective_day: 15,
        avg_amount: 1200, effective_avg: 1200, amounts: [9999, 0, 400, 400, 400], source: "hybrid",
        ...changes,
    };
}

function catalogEntry(changes: Partial<DriverCatalogEntry> = {}): DriverCatalogEntry {
    return { key: "customer:200", name: longName, entity_type: "customer", entity_id: "200", aliases: [], active: true,
        first_seen: "2026-10-01", last_seen: "2026-10-20", gl_line_count: 1, identity_basis: "gl_id", ...changes };
}
function catalogFixture(entries = [catalogEntry(), catalogEntry({ key: "vendor:300", name: "Synthetic vendor", entity_type: "vendor", entity_id: "300" })]): DriverCatalog {
    return { schema_version: 1, source: "GeneralLedger", company_id: "fixture-realm", start_date: "2026-05-03", end_date: "2026-10-29", observed_at: "2026-10-29T12:00:00Z",
        coverage: { status: "complete", issues: [], data_rows: entries.length, named_rows: entries.length, unattributed_rows: 0 }, entries };
}

function Harness({ customers = [entity()], vendors = [entity({ name: "Synthetic vendor", source: "pattern" })], selection = null, onSelect = () => {}, catalog = catalogFixture(), catalogLoading = false, catalogError = null, onRefreshCatalog = () => {} }: {
    customers?: DriverEntity[];
    vendors?: DriverEntity[];
    selection?: DriverSelection | null;
    onSelect?: (value: DriverSelection | null) => void;
    catalog?: DriverCatalog | null; catalogLoading?: boolean; catalogError?: string | null; onRefreshCatalog?: () => void;
}) {
    const [open, setOpen] = useState(false);
    const [selected, setSelected] = useState(selection);
    return <>
        <button onClick={() => setOpen(true)}>Inspect drivers</button>
        <DriversDrawer open={open} onClose={() => setOpen(false)} customers={customers} vendors={vendors}
            weekHeaders={weeks} lookback={1} selected={selected} onSelect={(value) => { onSelect(value); setSelected(value); }} catalog={catalog} catalogLoading={catalogLoading} catalogError={catalogError} onRefreshCatalog={onRefreshCatalog} />
    </>;
}

function definition(label: string) {
    return within(screen.getByRole("dialog", { name: "Drivers" })).getByText(label, { selector: "dt" }).nextElementSibling!;
}

afterEach(() => { vi.restoreAllMocks(); document.body.style.overflow = ""; });

describe("read-only cash-flow drivers", () => {
    it("renders nothing when closed", () => {
        const { container } = render(<DriversDrawer open={false} onClose={vi.fn()} customers={[entity()]} vendors={[]}
            weekHeaders={weeks} lookback={1} selected={null} onSelect={vi.fn()} catalog={null} catalogLoading={false} catalogError={null} onRefreshCatalog={vi.fn()} />);
        expect(container).toBeEmptyDOMElement();
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(document.body.style.overflow).toBe("");
    });

    it("opens a portal directory, selects by type/key, and returns keyboard focus to the selected entry", async () => {
        const user = userEvent.setup();
        const selected = vi.fn();
        const { container } = render(<Harness onSelect={selected} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        const dialog = screen.getByRole("dialog", { name: "Drivers" });
        expect(container).not.toContainElement(dialog);
        expect(dialog).toHaveAttribute("id", "cashflow-drivers-drawer");
        expect(dialog).toHaveAttribute("aria-modal", "true");
        expect(within(dialog).getByRole("region", { name: "Customers" })).toBeInTheDocument();
        expect(within(dialog).getByRole("region", { name: "Vendors" })).toBeInTheDocument();

        await user.click(within(dialog).getByRole("button", { name: `Inspect GL customer ${longName} (200)` }));
        expect(selected).toHaveBeenLastCalledWith({ type: "catalog", entryKey: "customer:200" });
        expect(within(dialog).getByRole("heading", { name: longName })).toHaveFocus();
        expect(definition("Day of month")).toHaveTextContent("15");
        expect(definition("Monthly run-rate")).toHaveTextContent("$1,200.00");
        expect(definition("Source")).toHaveTextContent("Open items and recurring run-rate");

        await user.click(within(dialog).getByRole("button", { name: "All drivers" }));
        expect(selected).toHaveBeenLastCalledWith(null);
        expect(within(dialog).getByRole("button", { name: `Inspect GL customer ${longName} (200)` })).toHaveFocus();
        await user.click(within(dialog).getByRole("button", { name: "Inspect GL vendor Synthetic vendor (300)" }));
        expect(selected).toHaveBeenLastCalledWith({ type: "catalog", entryKey: "vendor:300" });
        expect(within(dialog).getByRole("heading", { name: "Synthetic vendor" })).toBeInTheDocument();
    });

    it("contains keyboard focus, dismisses with Escape and restores the opener and background state", async () => {
        const user = userEvent.setup();
        document.body.style.overflow = "auto";
        const { container } = render(<Harness />);
        const opener = screen.getByRole("button", { name: "Inspect drivers" });
        await user.click(opener);
        const dialog = screen.getByRole("dialog", { name: "Drivers" });
        const close = within(dialog).getByRole("button", { name: "Close drivers" });
        const last = within(dialog).getByRole("button", { name: "Inspect GL vendor Synthetic vendor (300)" });
        expect(close).toHaveFocus();
        expect(container).toHaveAttribute("inert");
        expect(container).toHaveAttribute("aria-hidden", "true");
        expect(document.body.style.overflow).toBe("hidden");
        await user.keyboard("{Shift>}{Tab}{/Shift}");
        expect(last).toHaveFocus();
        await user.keyboard("{Tab}");
        expect(close).toHaveFocus();
        opener.focus();
        expect(close).toHaveFocus();
        await user.keyboard("{Escape}");
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(opener).toHaveFocus();
        expect(container).not.toHaveAttribute("inert");
        expect(container).not.toHaveAttribute("aria-hidden");
        expect(document.body.style.overflow).toBe("auto");
    });

    it.each(["close button", "backdrop"])("dismisses with the %s and returns focus", async (method) => {
        const user = userEvent.setup();
        render(<Harness />);
        const opener = screen.getByRole("button", { name: "Inspect drivers" });
        await user.click(opener);
        if (method === "close button") await user.click(screen.getByRole("button", { name: "Close drivers" }));
        else await user.click(screen.getByTestId("cashflow-drivers-backdrop"));
        await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
        expect(opener).toHaveFocus();
    });

    it("keeps zero, unavailable values and supplied future layers distinct", async () => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "vendor", rowKey: "row-a" }} vendors={[entity({
            name: "Synthetic hybrid row", source: "hybrid", day_of_month: null, effective_day: null,
            avg_amount: 0, effective_avg: 0, amounts: [9999, 0, 400, null, -200],
            open_invoices: 0, total_open_balance: 0, past_due_balance: null, dso_days: 0,
            has_historical_data: false, known_amounts: [9999, 0, 300, null, 0], run_rate_amounts: [0, 0, 100, null, -200],
        })]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        const dialog = screen.getByRole("dialog");
        expect(definition("Day of month")).toHaveTextContent("Unavailable");
        expect(definition("Monthly run-rate")).toHaveTextContent("$0.00");
        expect(definition("Open source items")).toHaveTextContent(/^0$/);
        expect(definition("Open balance")).toHaveTextContent("$0.00");
        expect(definition("Past-due balance")).toHaveTextContent("Unavailable");
        expect(definition("Source timing (days)")).toHaveTextContent(/^0$/);
        expect(definition("Historical data available")).toHaveTextContent("No");
        const table = within(dialog).getByRole("table");
        expect(within(table).queryByText("$9,999.00")).not.toBeInTheDocument();
        expect(within(table).queryByText("Oct 23, 2026")).not.toBeInTheDocument();
        const row = within(table).getByRole("row", { name: /Nov 6, 2026/ });
        expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["$400.00", "$300.00", "$100.00"]);
        const unavailable = within(table).getByRole("row", { name: /Nov 13, 2026/ });
        expect(within(unavailable).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Unavailable", "Unavailable", "Unavailable"]);
        expect(within(table).getAllByText("($200.00)")).not.toHaveLength(0);
    });

    it("does not replace a null current value with a source average or invent missing source details", async () => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "customer", rowKey: "row-a" }} customers={[entity({
            effective_day: null, effective_avg: null, source: undefined,
        })]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(definition("Day of month")).toHaveTextContent("Unavailable");
        expect(definition("Monthly run-rate")).toHaveTextContent("Unavailable");
        expect(definition("Source day of month")).toHaveTextContent("15");
        expect(definition("Source monthly average")).toHaveTextContent("$1,200.00");
        expect(definition("Source")).toHaveTextContent("Unavailable");
        expect(screen.queryByText("Additional source details were not supplied.")).not.toBeInTheDocument();
        expect(screen.queryByText("Open balance")).not.toBeInTheDocument();
        expect(screen.queryByRole("columnheader", { name: "Known" })).not.toBeInTheDocument();
    });

    it.each([["open_items", "Open items"], ["history", "Cash history"]])("presents the supplied %s run-rate source in plain language", async (source, label) => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "customer", rowKey: "row-a" }} customers={[entity({ run_rate_source: source })]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(definition("Run-rate source").textContent).toBe(label);
    });

    it("does not promote native manual-row defaults into historical or open-item evidence", async () => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "vendor", rowKey: "row-a" }} vendors={[entity({
            name: "Synthetic finite payment", source: "manual", avg_amount: 0, effective_avg: 0,
            amounts: [0, 400, 400, 400, 0], open_invoices: 0, total_open_balance: 0,
            past_due_balance: 0, run_rate_source: "history", dso_days: 0, has_historical_data: false,
            known_amounts: [0, 0, 0, 0, 0], run_rate_amounts: [0, 0, 0, 0, 0],
        })]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(definition("Source")).toHaveTextContent("Manual inputs");
        expect(definition("Monthly run-rate")).toHaveTextContent("$0.00");
        expect(screen.getByText("This row has weekly amounts even though its monthly run-rate is $0.00.")).toBeInTheDocument();
        const source = within(screen.getByRole("region", { name: "Source evidence" }));
        for (const label of ["Open source items", "Open balance", "Past-due balance", "Run-rate source", "Source timing (days)", "Historical data available", "Cash history"]) {
            expect(source.queryByText(label)).not.toBeInTheDocument();
        }
        expect(screen.queryByText("Additional source details were not supplied.")).not.toBeInTheDocument();
        const table = within(screen.getByRole("table"));
        expect(table.getAllByText("$400.00")).toHaveLength(3);
        expect(table.queryByRole("columnheader", { name: "Known" })).not.toBeInTheDocument();
        expect(table.queryByRole("columnheader", { name: "Run-rate" })).not.toBeInTheDocument();
    });

    it.each([["invoice", true, false], ["pattern", false, true]] as const)("limits %s evidence to its declared source", async (source, openItems, runRate) => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "customer", rowKey: "row-a" }} customers={[entity({
            source, open_invoices: 0, total_open_balance: 0, past_due_balance: 0,
            run_rate_source: "history", dso_days: 0, has_historical_data: true,
        })]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        const evidence = within(screen.getByRole("region", { name: "Source evidence" }));
        expect(Boolean(evidence.queryByText("Open balance"))).toBe(openItems);
        expect(Boolean(evidence.queryByText("Run-rate source"))).toBe(runRate);
    });

    it("shows a missing selection honestly instead of substituting a different entity", async () => {
        const user = userEvent.setup();
        render(<Harness selection={{ type: "customer", rowKey: "removed-row" }} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getByRole("heading", { name: "Driver unavailable" })).toHaveFocus();
        expect(screen.queryByRole("heading", { name: longName })).not.toBeInTheDocument();
        await user.click(screen.getByRole("button", { name: "All drivers" }));
        expect(screen.getByRole("region", { name: "Customers" })).toBeInTheDocument();
    });

    it("browses frozen evidence without making requests or mutating forecast rows", async () => {
        const user = userEvent.setup();
        const row = entity();
        Object.freeze(row.amounts); Object.freeze(row);
        const customers = [row]; Object.freeze(customers);
        const original = JSON.stringify(customers);
        const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected acquisition/write"));
        render(<Harness customers={customers} vendors={[]} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        await user.click(screen.getByRole("button", { name: `Inspect GL customer ${longName} (200)` }));
        await user.click(screen.getByRole("button", { name: "All drivers" }));
        await user.keyboard("{Escape}");
        expect(fetch).not.toHaveBeenCalled();
        expect(JSON.stringify(customers)).toBe(original);
    });

    it("includes inactive GL-only names without fabricating forecast amounts and searches observed aliases", async () => {
        const user = userEvent.setup();
        const ledger = catalogFixture([catalogEntry({ key: "vendor:inactive", name: "Dormant GL vendor", entity_type: "vendor", entity_id: "inactive", active: false, aliases: ["Earlier payee label"] })]);
        render(<Harness customers={[]} vendors={[]} catalog={ledger} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getByText("Dormant GL vendor")).toBeInTheDocument();
        expect(screen.getByText("Inactive")).toBeInTheDocument();
        expect(screen.getByText("Not in forecast")).toBeInTheDocument();
        expect(screen.queryByText("Unused master contact")).not.toBeInTheDocument();
        await user.type(screen.getByRole("searchbox"), "earlier payee");
        await user.click(screen.getByRole("button", { name: "Inspect GL vendor Dormant GL vendor (inactive)" }));
        expect(definition("Contact status")).toHaveTextContent("Inactive");
        expect(definition("Observed aliases")).toHaveTextContent("Earlier payee label");
        expect(definition("Source ID")).toHaveTextContent("inactive");
        expect(definition("GL line count")).toHaveTextContent(/^1$/);
        expect(screen.queryByText("Monthly run-rate")).not.toBeInTheDocument();
        expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
        expect(screen.queryByRole("table")).not.toBeInTheDocument();
    });

    it("derives forecast status from current future values and preserves distinct ambiguous identities", async () => {
        const user = userEvent.setup();
        const ledger = catalogFixture([
            catalogEntry({ key: "customer:scheduled", name: "Scheduled name", entity_id: "scheduled" }),
            catalogEntry({ key: "customer:zero", name: "Zero future", entity_id: "zero" }),
            catalogEntry({ key: "customer:unknown", name: "Unknown future", entity_id: "unknown" }),
            catalogEntry({ key: "customer:dup1", name: "Duplicate GL name", entity_id: "dup1" }),
            catalogEntry({ key: "customer:dup2", name: "Duplicate GL name", entity_id: "dup2" }),
            catalogEntry({ key: "unresolved:untyped", name: "Unresolved GL name", entity_type: "unresolved", entity_id: null, identity_basis: "unresolved" }),
        ]);
        render(<Harness vendors={[]} customers={[
            entity({ row_key: "scheduled", name: "Scheduled name" }),
            entity({ row_key: "zero", name: "Zero future", amounts: [9999, 0, 0, 0, 0] }),
            entity({ row_key: "unknown", name: "Unknown future", amounts: [9999, null, 0, 0, 0] }),
            entity({ row_key: "dup", name: "Duplicate GL name" }),
        ]} catalog={ledger} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getAllByText("In forecast · Scheduled")).toHaveLength(1);
        expect(screen.getByText("In forecast · No future cash")).toBeInTheDocument();
        expect(screen.getAllByText("Needs review").length).toBeGreaterThanOrEqual(4);
        expect(screen.getAllByRole("button", { name: /Inspect GL customer Duplicate GL name/ })).toHaveLength(2);
        expect(screen.getByRole("region", { name: "Names needing identity review" })).toHaveTextContent("Unresolved GL name");
        await user.click(screen.getByRole("button", { name: "Inspect GL customer Scheduled name (scheduled)" }));
        expect(screen.getByText("Matched by name · read-only link")).toBeInTheDocument();
    });

    it("keeps colliding manual assumptions separate and never labels missing GL coverage as complete", async () => {
        const user = userEvent.setup();
        const ledger = catalogFixture([catalogEntry()]);
        render(<Harness customers={[entity({ source: "manual" })]} vendors={[]} catalog={ledger} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getByRole("region", { name: "Customers" })).toHaveTextContent("Not in forecast");
        const other = within(screen.getByRole("region", { name: "Other forecast rows" }));
        expect(other.getByText(/Manual assumption · In forecast/, { selector: "span" })).toBeInTheDocument();
        await user.click(other.getByRole("button", { name: `Inspect customer ${longName} driver` }));
        expect(definition("Source")).toHaveTextContent("Manual inputs");
    });

    it("shows unavailable and partial GL coverage honestly while retaining separately labelled forecast rows", async () => {
        const user = userEvent.setup();
        const { rerender } = render(<Harness catalog={null} catalogError="Source read failed" />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getByRole("alert")).toHaveTextContent("GL directory unavailable");
        expect(screen.queryByRole("region", { name: "Customers" })).not.toBeInTheDocument();
        expect(screen.getByRole("region", { name: "Other forecast rows" })).toBeInTheDocument();
        const partial = catalogFixture([]);
        partial.coverage = { status: "partial", issues: [{ code: "missing", message: "Some GL pages were unavailable" }], data_rows: 0, named_rows: 0, unattributed_rows: 0 };
        rerender(<Harness catalog={partial} />);
        expect(screen.getByText("Partial read · names may be missing")).toBeInTheDocument();
        expect(screen.getByText("Some GL pages were unavailable")).toBeInTheDocument();
        expect(screen.queryByText("No customer or vendor names were observed in this GL window.")).not.toBeInTheDocument();
    });

    it("keeps the stale-read notice visible while inspecting a retained GL entry", async () => {
        const user = userEvent.setup();
        render(<Harness catalogError="The latest read was incomplete" />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        await user.click(screen.getByRole("button", { name: `Inspect GL customer ${longName} (200)` }));
        expect(screen.getByRole("alert")).toHaveTextContent("Showing the retained directory; it may be stale");
        expect(screen.getByRole("alert")).toHaveTextContent("The latest read was incomplete");
        expect(screen.getByText("Retained GL read")).toBeInTheDocument();
    });

    it("returns focus inside the directory when the inspected GL entry is no longer present", async () => {
        const user = userEvent.setup();
        const { rerender } = render(<Harness />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        await user.click(screen.getByRole("button", { name: `Inspect GL customer ${longName} (200)` }));
        rerender(<Harness catalog={catalogFixture([])} />);
        expect(screen.getByRole("heading", { name: "Driver unavailable" })).toBeInTheDocument();
        await user.click(screen.getByRole("button", { name: "All drivers" }));
        expect(screen.getByRole("button", { name: "Close drivers" })).toHaveFocus();
    });

    it("makes every large-directory name reachable with explicit counts and alias search", async () => {
        const user = userEvent.setup();
        const ledger = catalogFixture(Array.from({ length: 51 }, (_, index) => catalogEntry({ key: `customer:${index}`, name: `GL customer ${index}`, entity_id: String(index), aliases: index === 50 ? ["Last alias"] : [] })));
        render(<Harness customers={[]} vendors={[]} catalog={ledger} />);
        await user.click(screen.getByRole("button", { name: "Inspect drivers" }));
        expect(screen.getByText("Showing 1–50 of 51")).toBeInTheDocument();
        expect(screen.queryByText("GL customer 50")).not.toBeInTheDocument();
        await user.click(screen.getByRole("button", { name: "Next Customers" }));
        expect(screen.getByText("Showing 51–51 of 51")).toBeInTheDocument();
        expect(screen.getByText("GL customer 50")).toBeInTheDocument();
        await user.type(screen.getByRole("searchbox"), "last alias");
        expect(screen.getByText("GL customer 50")).toBeInTheDocument();
        expect(screen.queryByText("GL customer 0")).not.toBeInTheDocument();
    });
});
