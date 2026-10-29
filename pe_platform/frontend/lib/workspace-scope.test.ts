import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore, type LocalBootstrap } from "./store";
import { currentWorkspaceScope, isCurrentScope, registerWorkspaceStream } from "./workspace-scope";

const fixture = (id = "workspace-a", revision = 1): LocalBootstrap => ({
    workspace: { id, revision, realm_id: `realm-${id}`, organization_id: "local-org", name: id, currency: "USD", synthetic: true },
    workspaces: [], csrf_token: "fixture-csrf", data_directory: "/synthetic-fixture", source_revision: "fixture-revision",
});
beforeEach(() => useAppStore.getState().adoptBootstrap(fixture()));
describe("local workspace ownership", () => {
    it("aborts a live stream and invalidates captured context before selection completes", () => {
        const original = currentWorkspaceScope();
        const controller = new AbortController();
        const remove = registerWorkspaceStream(controller);
        useAppStore.getState().beginWorkspaceTransition();
        expect(controller.signal.aborted).toBe(true);
        expect(isCurrentScope(original)).toBe(false);
        expect(useAppStore.getState().activeCompanyId).toBeNull();
        useAppStore.getState().adoptBootstrap(fixture("workspace-b", 2));
        expect(isCurrentScope(original)).toBe(false);
        expect(useAppStore.getState().activeCompanyId).toBe("realm-workspace-b");
        remove();
    });
    it("invalidates a prior selection of the same workspace when its binding revision changes", () => {
        const original = currentWorkspaceScope();
        const controller = new AbortController();
        registerWorkspaceStream(controller);
        useAppStore.getState().adoptBootstrap(fixture("workspace-a", 3));
        expect(isCurrentScope(original)).toBe(false);
        expect(controller.signal.aborted).toBe(true);
    });
});
