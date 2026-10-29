import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("@/lib/store", () => ({ useAppStore: (selector: (state: unknown) => unknown) => selector({ workspace: { name: "Synthetic fixture", currency: "USD" } }) }));
vi.mock("@/lib/api/client", () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
import { apiClient } from "@/lib/api/client";
import ProposalReview from "./page";
const proposal = { id: "proposal-fixture", action_type: "CASH_FORECAST_SPREAD", status: "PENDING", payload: { total_amount: 1000 }, summary: "Fixture cash spread", stale: false, review_fingerprint: "detail-fingerprint", base_fingerprint: "base", current_fingerprint: "base" };
const forecast = { week_headers: ["2026-10-30"], beginning_balances: [1000], total_cash_in: [0], total_cash_out: [100], ending_balances: [900] };
beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(apiClient.get).mockImplementation(async (url) => ({ data: String(url) === "/local/proposals" ? { proposals: [proposal] } : proposal }));
});
async function open() {
    render(<ProposalReview />);
    fireEvent.click(await screen.findByRole("button", { name: /Fixture cash spread/ }));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith("/local/proposals/proposal-fixture/preview", { review_fingerprint: "detail-fingerprint" }));
}
describe("exact model proposal review", () => {
    it("keeps approval disabled when the deterministic preview is unavailable", async () => {
        vi.mocked(apiClient.post).mockResolvedValue({ data: { available: false, reason: "Source coverage is incomplete." } });
        await open();
        expect(await screen.findByText("Source coverage is incomplete.")).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Approve exact proposal" })).toBeDisabled();
        expect(screen.getByRole("checkbox")).toBeDisabled();
    });
    it("requires the explicit review and sends the exact freshly previewed token", async () => {
        vi.mocked(apiClient.post).mockImplementation(async (url) => ({ data: String(url).endsWith("/preview") ? { available: true, before: forecast, after: { ...forecast, ending_balances: [800], total_cash_out: [200] }, comparison: { ending_cash_delta: -100 }, review_fingerprint: "preview-fingerprint", preview_token: "exact-preview-token" } : { status: "EXECUTED" } }));
        await open();
        expect(await screen.findByText("Deterministic forecast impact")).toBeInTheDocument();
        const approve = screen.getByRole("button", { name: "Approve exact proposal" });
        expect(approve).toBeDisabled();
        fireEvent.click(screen.getByRole("checkbox"));
        expect(approve).toBeEnabled();
        fireEvent.click(approve);
        await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith("/local/proposals/proposal-fixture/approve", { review_fingerprint: "preview-fingerprint", preview_token: "exact-preview-token" }));
    });
});
