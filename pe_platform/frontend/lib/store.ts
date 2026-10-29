"use client";

import { create } from "zustand";

export interface LocalWorkspace {
    id: string;
    organization_id: string | number;
    realm_id: string;
    name: string;
    currency: string;
    synthetic: boolean;
    revision: number;
}

export interface LocalBootstrap {
    workspace: LocalWorkspace | null;
    workspaces: LocalWorkspace[];
    csrf_token: string;
    data_directory: string;
    source_revision: string;
}

interface AppState {
    workspace: LocalWorkspace | null;
    workspaces: LocalWorkspace[];
    ready: boolean;
    assistantEpoch: number;
    resetAssistantContext: () => void;
    activeCompanyId: string | null;
    activeCompanyName: string | null;
    activeOrgId: string | null;
    activeMonth: number;
    activeYear: number;
    dataDirectory: string;
    sourceRevision: string;
    adoptBootstrap: (value: LocalBootstrap) => void;
    beginWorkspaceTransition: () => void;
    setActivePeriod: (month: number, year: number) => void;
}

// Local adapter: company identity is accepted only from the backend bootstrap.
// Nothing in this store, including workspace identity, is persisted in browser storage.
export const useAppStore = create<AppState>((set) => ({
    workspace: null,
    workspaces: [],
    ready: false,
    assistantEpoch: 0,
    resetAssistantContext: () => set((state) => ({ assistantEpoch: state.assistantEpoch + 1 })),
    activeCompanyId: null,
    activeCompanyName: null,
    activeOrgId: null,
    activeMonth: new Date().getMonth() + 1,
    activeYear: new Date().getFullYear(),
    dataDirectory: "",
    sourceRevision: "",
    adoptBootstrap: (value) => set({
        workspace: value.workspace,
        workspaces: value.workspaces,
        activeCompanyId: value.workspace?.realm_id ?? null,
        activeCompanyName: value.workspace?.name ?? null,
        activeOrgId: value.workspace ? String(value.workspace.organization_id) : null,
        dataDirectory: value.data_directory,
        sourceRevision: value.source_revision,
        ready: true,
    }),
    beginWorkspaceTransition: () => set({
        workspace: null, activeCompanyId: null, activeCompanyName: null,
        activeOrgId: null, ready: false,
    }),
    setActivePeriod: (month, year) => set({ activeMonth: month, activeYear: year }),
}));
