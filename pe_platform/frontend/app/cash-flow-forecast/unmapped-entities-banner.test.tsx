// PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-4 (F-4 — banner tests).
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Mock the API seam so the component's render/interaction branches are under test.
vi.mock("@/lib/api/cff-integrity", () => ({
  getUnmappedEntities: vi.fn(),
}));

import { getUnmappedEntities, UnmappedEntity } from "@/lib/api/cff-integrity";
import { UnmappedEntitiesBanner } from "./unmapped-entities-banner";

const GHOST_VENDOR: UnmappedEntity = {
  entity_name: "Ghost Vendor",
  entity_type: "vendor",
  transaction_count: 2,
  total_amount: 250.5,
  first_week_ending: "2026-06-12",
  last_week_ending: "2026-06-19",
};

const GHOST_CUSTOMER: UnmappedEntity = {
  entity_name: "Ghost Customer",
  entity_type: "customer",
  transaction_count: 1,
  total_amount: 75.25,
  first_week_ending: "2026-06-19",
  last_week_ending: "2026-06-19",
};

function mockList(unmapped: UnmappedEntity[]) {
  vi.mocked(getUnmappedEntities).mockResolvedValue({
    company_id: "realm-1",
    since_weeks: 26,
    count: unmapped.length,
    unmapped,
  });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("UnmappedEntitiesBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when there are no unmapped entities", async () => {
    mockList([]);
    render(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={vi.fn()} />);
    await waitFor(() => expect(getUnmappedEntities).toHaveBeenCalledWith("realm-1"));
    expect(screen.queryByTestId("cff-unmapped-banner")).not.toBeInTheDocument();
  });

  it("renders nothing when the detection call fails (advisory, never breaks the page)", async () => {
    vi.mocked(getUnmappedEntities).mockRejectedValue(new Error("boom"));
    render(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={vi.fn()} />);
    await waitFor(() => expect(getUnmappedEntities).toHaveBeenCalled());
    expect(screen.queryByTestId("cff-unmapped-banner")).not.toBeInTheDocument();
  });

  it("lists each unmapped entity with its stake", async () => {
    mockList([GHOST_VENDOR, GHOST_CUSTOMER]);
    render(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={vi.fn()} />);

    expect(await screen.findByTestId("cff-unmapped-banner")).toBeInTheDocument();
    expect(screen.getByText(/2 categorized entities aren't in this forecast/)).toBeInTheDocument();
    expect(screen.getByText("Ghost Vendor")).toBeInTheDocument();
    expect(screen.getByText(/vendor · 2 transactions ·/)).toBeInTheDocument();
    expect(screen.getByText(/\$250\.50/)).toBeInTheDocument();
    expect(screen.getByText("Ghost Customer")).toBeInTheDocument();
  });

  it("one-click Add row creates the row, removes the entity, and notifies the parent", async () => {
    mockList([GHOST_VENDOR, GHOST_CUSTOMER]);
    const onAddRow = vi.fn().mockResolvedValue(undefined);
    render(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={onAddRow} />);

    await screen.findByTestId("cff-unmapped-banner");
    fireEvent.click(screen.getAllByRole("button", { name: "Add row" })[0]);

    await waitFor(() =>
      expect(onAddRow).toHaveBeenCalledWith(GHOST_VENDOR),
    );
    expect(screen.queryByText("Ghost Vendor")).not.toBeInTheDocument();
    // The other entity stays flagged.
    expect(screen.getByText("Ghost Customer")).toBeInTheDocument();
  });

  it("a failed add keeps the entity listed and surfaces the error", async () => {
    mockList([GHOST_VENDOR]);
    const onAddRow = vi.fn().mockRejectedValue(new Error("nope"));
    render(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={onAddRow} />);

    await screen.findByTestId("cff-unmapped-banner");
    fireEvent.click(screen.getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(onAddRow).toHaveBeenCalled());
    expect(screen.getByText("Ghost Vendor")).toBeInTheDocument();
    expect(screen.getByText(/Couldn't add a forecast row|nope/)).toBeInTheDocument();
  });

  it("dismiss hides the banner for the CURRENT entity set only", async () => {
    mockList([GHOST_VENDOR]);
    const { rerender } = render(
      <UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={0} onAddRow={vi.fn()} />,
    );
    await screen.findByTestId("cff-unmapped-banner");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss unmapped entities callout" }));
    expect(screen.queryByTestId("cff-unmapped-banner")).not.toBeInTheDocument();

    // Re-checking the SAME entity set preserves the keyed dismissal.
    rerender(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={1} onAddRow={vi.fn()} />);
    await waitFor(() => expect(getUnmappedEntities).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("cff-unmapped-banner")).not.toBeInTheDocument();

    // A NEW unmapped entity appears → the banner returns (dismissal is signature-keyed).
    mockList([GHOST_VENDOR, GHOST_CUSTOMER]);
    rerender(<UnmappedEntitiesBanner companyId="realm-1" requestGeneration={1} refreshKey={2} onAddRow={vi.fn()} />);
    expect(await screen.findByTestId("cff-unmapped-banner")).toBeInTheDocument();
  });

  it("ignores an old A-generation add completion after an A → B → A switch", async () => {
    mockList([GHOST_VENDOR]);
    const oldAdd = deferred<void>();
    const oldOnAddRow = vi.fn(() => oldAdd.promise);
    const view = render(
      <UnmappedEntitiesBanner
        companyId="realm-a"
        requestGeneration={1}
        refreshKey={0}
        onAddRow={oldOnAddRow}
      />,
    );
    await screen.findByText("Ghost Vendor");
    fireEvent.click(screen.getByRole("button", { name: "Add row" }));
    expect(oldOnAddRow).toHaveBeenCalledOnce();

    view.rerender(
      <UnmappedEntitiesBanner
        companyId="realm-b"
        requestGeneration={2}
        refreshKey={0}
        onAddRow={vi.fn()}
      />,
    );
    view.rerender(
      <UnmappedEntitiesBanner
        companyId="realm-a"
        requestGeneration={3}
        refreshKey={0}
        onAddRow={vi.fn()}
      />,
    );
    expect(await screen.findByText("Ghost Vendor")).toBeInTheDocument();

    await act(async () => {
      oldAdd.resolve();
      await oldAdd.promise;
    });
    expect(screen.getByText("Ghost Vendor")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add row" })).toBeEnabled();
  });
});
