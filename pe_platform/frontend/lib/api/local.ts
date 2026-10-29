import { apiClient, setCsrfFallbackToken } from "./client";
import { useAppStore, type LocalBootstrap } from "../store";
import { StaleWorkspaceError } from "../workspace-scope";

let generation = 0;

function validateBootstrap(value: LocalBootstrap) {
    if (!value || !Array.isArray(value.workspaces) || typeof value.csrf_token !== "string" || !value.csrf_token || typeof value.data_directory !== "string" || typeof value.source_revision !== "string") {
        throw new Error("The local backend returned an incomplete workspace bootstrap. Check the configured app address.");
    }
    const workspaces = [...value.workspaces, ...(value.workspace ? [value.workspace] : [])];
    if (workspaces.some((workspace) => !workspace || !workspace.id || !workspace.realm_id || !workspace.name || workspace.currency !== "USD" || !Number.isInteger(workspace.revision))) {
        throw new Error("The local backend returned an invalid native workspace identity.");
    }
    return value;
}

export async function bootstrapLocal() {
    const admitted = ++generation;
    const { data } = await apiClient.get<LocalBootstrap>("/local/bootstrap");
    if (admitted !== generation) throw new StaleWorkspaceError();
    const verified = validateBootstrap(data);
    setCsrfFallbackToken(verified.csrf_token);
    useAppStore.getState().adoptBootstrap(verified);
    return data;
}
export async function selectLocalWorkspace(id: string) {
    const admitted = ++generation;
    useAppStore.getState().beginWorkspaceTransition();
    try {
        await apiClient.post("/local/workspaces/select", { id });
        if (admitted !== generation) throw new StaleWorkspaceError();
        await bootstrapLocal();
    } catch (error) {
        await bootstrapLocal();
        throw error;
    }
}
export async function createLocalWorkspace(name: string, currency: string) {
    await apiClient.post("/local/workspaces", { name, currency });
    return bootstrapLocal();
}
export async function createLocalDemo() {
    await apiClient.post("/local/demo", {});
    return bootstrapLocal();
}
