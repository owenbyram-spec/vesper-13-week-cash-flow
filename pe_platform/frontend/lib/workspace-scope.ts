"use client";

import { useAppStore } from "./store";

export interface WorkspaceScope { id: string; revision: number; realmId: string }
export function currentWorkspaceScope(): WorkspaceScope | null {
    const workspace = useAppStore.getState().workspace;
    return workspace ? { id: workspace.id, revision: workspace.revision, realmId: workspace.realm_id } : null;
}
export function scopeKey(scope: WorkspaceScope | null) {
    return scope ? `${scope.id}:${scope.revision}:${scope.realmId}` : "none";
}
export function isCurrentScope(scope: WorkspaceScope | null) {
    return scopeKey(scope) === scopeKey(currentWorkspaceScope());
}
export class StaleWorkspaceError extends Error {
    constructor() { super("The workspace changed. Repeat this action in the selected workspace."); }
}
const activeStreams = new Set<AbortController>();
useAppStore.subscribe((state, previous) => {
    const next = state.workspace;
    const old = previous.workspace;
    if (next?.id !== old?.id || next?.revision !== old?.revision || state.assistantEpoch !== previous.assistantEpoch) {
        for (const controller of activeStreams) controller.abort();
        activeStreams.clear();
    }
});
export function registerWorkspaceStream(controller: AbortController) {
    activeStreams.add(controller);
    return () => activeStreams.delete(controller);
}
