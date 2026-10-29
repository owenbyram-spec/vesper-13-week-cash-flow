import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("@/lib/store", () => ({ useAppStore: (selector: (state: unknown) => unknown) => selector({ workspace: { name: "Synthetic fixture", currency: "USD", synthetic: true }, dataDirectory: "/fixture", sourceRevision: "fixture-source" }) }));
vi.mock("@/lib/api/local", () => ({ bootstrapLocal: vi.fn() }));
vi.mock("@/lib/api/client", () => ({ apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() } }));
import { apiClient } from "@/lib/api/client";
import Connections from "./page";
const status = (configured = false) => ({ model: { configured }, qbo: { configured: false } });
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiClient.get).mockResolvedValue({ data: status() }); vi.mocked(apiClient.post).mockResolvedValue({ data: { ok: true } }); });
async function fillAndSave(persist = false) {
    render(<Connections />);
    await waitFor(() => expect(apiClient.get).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "fixture-model" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "fixture-secret" } });
    fireEvent.click(screen.getByLabelText(/I authorise this external data flow/));
    if (persist) fireEvent.click(screen.getByLabelText("Store in OS credential store; otherwise keep only in this backend process."));
    fireEvent.click(screen.getByRole("button", { name: "Save AI connection" }));
}
describe("local connection save confirmation", () => {
    it("retains the masked draft and blocks Test when a successful POST is not confirmed by status", async () => {
        await fillAndSave();
        expect(await screen.findByRole("alert")).toHaveTextContent("Save was not confirmed");
        expect(screen.getByLabelText("API key")).toHaveValue("fixture-secret");
        expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
        expect(screen.getByRole("button", { name: "Test AI connection" })).toBeDisabled();
    });
    it("clears the draft only after confirmed status and disables Test again for an unsaved edit", async () => {
        vi.mocked(apiClient.get).mockResolvedValueOnce({ data: status() }).mockResolvedValue({ data: { ...status(true), api_key: "must-never-display", model: { configured: true, provider: "openai", model: "fixture-model", api_key: "must-never-display" } } });
        await fillAndSave();
        await waitFor(() => expect(screen.getByRole("button", { name: "Test AI connection" })).toBeEnabled());
        expect(screen.getByLabelText("API key")).toHaveValue("");
        expect(screen.queryByText(/must-never-display/)).not.toBeInTheDocument();
        fireEvent.change(screen.getByLabelText("Model"), { target: { value: "changed-model" } });
        expect(screen.getByRole("button", { name: "Test AI connection" })).toBeDisabled();
    });
});


describe("credential storage notices", () => {
    it("shows the safe locked-store notice and retains the masked draft when persistent saving fails", async () => {
        const warning = "The OS credential store could not be read. Unlock it to use saved connections, or reconnect for this session. Existing saved credentials have not been removed.";
        vi.mocked(apiClient.get).mockResolvedValue({ data: { ...status(), storage_warning: warning } });
        vi.mocked(apiClient.post).mockRejectedValue({ response: { status: 400, data: { detail: "The OS credential store could not save the credentials; choose session-only storage." } } });
        await fillAndSave(true);
        expect(screen.getByRole("alert", { name: "Credential storage notice" })).toHaveTextContent(warning);
        expect(await screen.findByText("The OS credential store could not save the credentials; choose session-only storage.")).toBeInTheDocument();
        expect(apiClient.post).toHaveBeenCalledWith("/local/connections/model", expect.objectContaining({ persist: true }));
        expect(screen.getByLabelText("API key")).toHaveValue("fixture-secret");
        expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
        expect(screen.getByRole("button", { name: "Test AI connection" })).toBeDisabled();
    });
    it.each(["model", "qbo"])("keeps the prior OS-copy uncertainty visible for an active %s session connection", async (kind) => {
        const warning = "This connection is available for this session. Previously saved OS credentials, if any, were not checked or removed. Unlock the credential store and remove the connection to clear a saved copy.";
        vi.mocked(apiClient.get).mockResolvedValue({ data: { ...status(), [kind]: { configured: true, storage_mode: "session-only", api_key: "must-never-display", refresh_token: "must-never-display" }, storage_warning: warning } });
        render(<Connections />);
        expect(await screen.findByRole("alert", { name: "Credential storage notice" })).toHaveTextContent(warning);
        expect(screen.getByRole("button", { name: kind === "model" ? "Test AI connection" : "Test company" })).toBeEnabled();
        expect(screen.queryByText(/must-never-display/)).not.toBeInTheDocument();
    });
    it("ignores unexpected structured storage warnings rather than rendering their fields", async () => {
        vi.mocked(apiClient.get).mockResolvedValue({ data: { ...status(), storage_warning: { api_key: "must-never-display" } } });
        render(<Connections />);
        await waitFor(() => expect(apiClient.get).toHaveBeenCalledOnce());
        expect(screen.queryByRole("alert", { name: "Credential storage notice" })).not.toBeInTheDocument();
        expect(screen.queryByText(/must-never-display/)).not.toBeInTheDocument();
    });
});
