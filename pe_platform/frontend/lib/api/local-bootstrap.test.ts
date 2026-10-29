import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./client", () => ({ apiClient: { get: vi.fn(), post: vi.fn() }, setCsrfFallbackToken: vi.fn() }));
import { apiClient, setCsrfFallbackToken } from "./client";
import { bootstrapLocal, selectLocalWorkspace } from "./local";
import { useAppStore, type LocalBootstrap } from "../store";
import { StaleWorkspaceError } from "../workspace-scope";
const fixture = (id: string, revision: number): LocalBootstrap => ({ workspace: { id, revision, realm_id: `realm-${id}`, organization_id: "local", name: id, currency: "USD", synthetic: true }, workspaces: [{ id, revision, realm_id: `realm-${id}`, organization_id: "local", name: id, currency: "USD", synthetic: true }], csrf_token: "fixture-current-csrf", data_directory: "/fixture-data", source_revision: "fixture-source" });
beforeEach(() => { vi.resetAllMocks(); useAppStore.getState().adoptBootstrap(fixture("a", 1)); });
describe("authoritative local bootstrap adoption", () => {
    it("refetches the full bootstrap after a selection endpoint returns only its workspace", async () => {
        vi.mocked(apiClient.post).mockResolvedValue({ data: { workspace: fixture("b", 2).workspace } });
        vi.mocked(apiClient.get).mockResolvedValue({ data: fixture("b", 2) });
        await selectLocalWorkspace("b");
        expect(apiClient.get).toHaveBeenCalledWith("/local/bootstrap");
        expect(setCsrfFallbackToken).toHaveBeenCalledWith("fixture-current-csrf");
        expect(useAppStore.getState().workspace?.id).toBe("b");
        expect(useAppStore.getState().workspaces).toHaveLength(1);
        expect(useAppStore.getState().dataDirectory).toBe("/fixture-data");
    });
    it("never adopts an older bootstrap response over a newer selected workspace", async () => {
        let finishOld!: (value: { data: LocalBootstrap }) => void;
        vi.mocked(apiClient.get).mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; })).mockResolvedValueOnce({ data: fixture("b", 3) });
        const old = bootstrapLocal();
        const rejected = expect(old).rejects.toBeInstanceOf(StaleWorkspaceError);
        await bootstrapLocal();
        finishOld({ data: fixture("a", 1) });
        await rejected;
        expect(useAppStore.getState().workspace?.id).toBe("b");
    });
});
