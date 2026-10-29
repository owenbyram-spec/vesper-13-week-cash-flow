import { beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from "axios";
import { apiClient, setCsrfFallbackToken } from "./client";
import { useAppStore } from "../store";
import { StaleWorkspaceError } from "../workspace-scope";

function adopt(id = "workspace-a", revision = 7) {
    useAppStore.getState().adoptBootstrap({ workspace: { id, revision, realm_id: `realm-${id}`, organization_id: "local", name: id, currency: "USD", synthetic: true }, workspaces: [], csrf_token: "fixture-csrf", data_directory: "/fixture", source_revision: "fixture" });
}
const response = (config: InternalAxiosRequestConfig): AxiosResponse => ({ data: {}, status: 200, statusText: "OK", headers: {}, config });
beforeEach(() => { adopt(); setCsrfFallbackToken("fixture-csrf"); });
describe("same-origin local API context", () => {
    it("sends the displayed workspace revision and CSRF with native mutations", async () => {
        const adapter = vi.fn<AxiosAdapter>(async (config) => response(config));
        await apiClient.post("/cashflow-forecast/override", { company_id: "realm-workspace-a" }, { adapter });
        const headers = adapter.mock.calls[0][0].headers;
        expect(headers.get("X-Vesper-Workspace")).toBe("workspace-a");
        expect(headers.get("X-Vesper-Revision")).toBe("7");
        expect(headers.get("X-CSRF-Token")).toBe("fixture-csrf");
        expect(headers.get("Authorization")).toBeUndefined();
    });
    it("sends CSRF on the native GET that pulls and persists actuals", async () => {
        const adapter = vi.fn<AxiosAdapter>(async (config) => response(config));
        const epoch = useAppStore.getState().assistantEpoch;
        await apiClient.get("/cashflow-forecast/actuals/pull", { adapter });
        expect(useAppStore.getState().assistantEpoch).toBe(epoch + 1);
        expect(adapter.mock.calls[0][0].headers.get("X-CSRF-Token")).toBe("fixture-csrf");
        expect(adapter.mock.calls[0][0].headers.get("X-Vesper-Workspace")).toBe("workspace-a");
    });
    it("clears only assistant context and aborts streams after a successful financial mutation", async () => {
        const original = useAppStore.getState();
        const controller = new AbortController();
        const { registerWorkspaceStream } = await import("../workspace-scope");
        registerWorkspaceStream(controller);
        const adapter = vi.fn<AxiosAdapter>(async (config) => response(config));
        await apiClient.post("/cashflow-forecast/override", {}, { adapter });
        expect(useAppStore.getState().assistantEpoch).toBe(original.assistantEpoch + 1);
        expect(useAppStore.getState().workspace).toEqual(original.workspace);
        expect(controller.signal.aborted).toBe(true);
    });
    it("keeps valid chat context on failed mutations, previews and non-financial connection tests", async () => {
        const epoch = useAppStore.getState().assistantEpoch;
        const failed = vi.fn<AxiosAdapter>(async () => { throw new Error("Fixture rejected mutation"); });
        await expect(apiClient.post("/cashflow-forecast/override", {}, { adapter: failed })).rejects.toThrow("Fixture rejected");
        const success = vi.fn<AxiosAdapter>(async (config) => response(config));
        await apiClient.post("/local/connections/model/test", {}, { adapter: success });
        await apiClient.post("/local/proposals/fixture/preview", {}, { adapter: success });
        const notApplied = vi.fn<AxiosAdapter>(async (config) => ({ ...response(config), data: { status: "FAILED", executed: false } }));
        await apiClient.post("/local/proposals/fixture/approve", {}, { adapter: notApplied });
        expect(useAppStore.getState().assistantEpoch).toBe(epoch);
    });
    it("keeps bootstrap and selection independent from the old displayed scope", async () => {
        const adapter = vi.fn<AxiosAdapter>(async (config) => response(config));
        await apiClient.post("/local/workspaces/select", { id: "workspace-b" }, { adapter });
        expect(adapter.mock.calls[0][0].headers.get("X-Vesper-Workspace")).toBeUndefined();
        expect(adapter.mock.calls[0][0].headers.get("X-CSRF-Token")).toBe("fixture-csrf");
    });
    it("blocks an old component's company request before it reaches the network", async () => {
        const adapter = vi.fn<AxiosAdapter>(async (config) => response(config));
        adopt("workspace-b", 8);
        await expect(apiClient.get("/agent/runs?company_id=realm-workspace-a", { adapter })).rejects.toBeInstanceOf(StaleWorkspaceError);
        expect(adapter).not.toHaveBeenCalled();
    });
    it("rejects a late response after workspace selection changes", async () => {
        let complete!: (value: AxiosResponse) => void;
        let admitted!: InternalAxiosRequestConfig;
        const adapter = vi.fn<AxiosAdapter>((config) => { admitted = config; return new Promise((resolve) => { complete = resolve; }); });
        const request = apiClient.get("/cashflow-forecast/weekly", { adapter });
        const rejected = expect(request).rejects.toBeInstanceOf(StaleWorkspaceError);
        await waitFor(() => expect(adapter).toHaveBeenCalledOnce());
        adopt("workspace-b", 8);
        complete(response(admitted));
        await rejected;
    });
});
