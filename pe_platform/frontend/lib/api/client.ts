"use client";

import axios, { type InternalAxiosRequestConfig } from "axios";
import { useAppStore } from "../store";
import { currentWorkspaceScope, isCurrentScope, StaleWorkspaceError, type WorkspaceScope } from "../workspace-scope";

let csrfToken: string | null = null;
export class BackendConfigurationError extends Error {
    userMessage = "The local backend is unavailable. Check the launcher's printed address and data directory.";
}
export function getApiBaseUrl() { return "/bff"; }
export function getCsrfToken() { return csrfToken; }
export function setCsrfFallbackToken(token?: string | null) { csrfToken = token || null; }
export function persistAuthResponseTokens(data?: { csrf_token?: string | null }) {
    if (data?.csrf_token) csrfToken = data.csrf_token;
}
export function clearAuthResponseTokens() { csrfToken = null; }
export function buildApiHeaders(method = "GET", extra: Record<string, string> = {}) {
    const headers = { ...extra };
    const scope = currentWorkspaceScope();
    if (scope) {
        headers["X-Vesper-Workspace"] = scope.id;
        headers["X-Vesper-Revision"] = String(scope.revision);
    }
    if (csrfToken) {
        headers["X-CSRF-Token"] = csrfToken;
    }
    return headers;
}
type ScopedConfig = InternalAxiosRequestConfig & { _localScope?: WorkspaceScope | null; _scopeExempt?: boolean };
export const apiClient = axios.create({ baseURL: "/bff", withCredentials: true, headers: { "Content-Type": "application/json" } });
apiClient.interceptors.request.use((incoming) => {
    const config = incoming as ScopedConfig;
    config._scopeExempt = ["/local/bootstrap", "/local/workspaces/select"].includes((config.url || "").split("?")[0]);
    config._localScope = currentWorkspaceScope();
    const path = (config.url || "").split("?")[0];
    const initialCreate = ["/local/workspaces", "/local/demo"].includes(path);
    if (!config._scopeExempt && !config._localScope && !initialCreate) throw new StaleWorkspaceError();
    if (!config._scopeExempt && config._localScope) {
        const search = new URL(config.url || "/", "http://local.invalid").searchParams;
        const params = config.params as Record<string, unknown> | URLSearchParams | undefined;
        const body = config.data && typeof config.data === "object" ? config.data as Record<string, unknown> : {};
        for (const name of ["company_id", "realm_id"]) {
            const parameter = params instanceof URLSearchParams ? params.get(name) : params?.[name];
            const values = [search.get(name), parameter, ...(path === "/local/connections/qbo" ? [] : [body[name]])];
            if (values.some((value) => value != null && String(value) !== config._localScope?.realmId)) throw new StaleWorkspaceError();
        }
    }
    const headers = buildApiHeaders(config.method || "GET");
    for (const [key, value] of Object.entries(headers)) config.headers.set(key, value);
    if (config._scopeExempt) {
        config.headers.delete("X-Vesper-Workspace");
        config.headers.delete("X-Vesper-Revision");
    }
    return config;
});
function appliesFinancialChange(config: InternalAxiosRequestConfig, data: unknown) {
    const path = (config.url || "").split("?")[0];
    const method = (config.method || "GET").toUpperCase();
    const record = data && typeof data === "object" ? data as Record<string, unknown> : {};
    const internal = record.internal_execution && typeof record.internal_execution === "object" ? record.internal_execution as Record<string, unknown> : {};
    if (record.success === false || record.executed === false || internal.executed === false || String(record.status || "").toLowerCase() === "failed") return false;
    return (path.startsWith("/cashflow-forecast/") && !["GET", "HEAD", "OPTIONS"].includes(method))
        || (method === "GET" && path === "/cashflow-forecast/actuals/pull")
        || (method === "POST" && /^\/local\/proposals\/[^/]+\/approve$/.test(path));
}
apiClient.interceptors.response.use((response) => {
    const config = response.config as ScopedConfig;
    if (!config._scopeExempt && !isCurrentScope(config._localScope ?? null)) throw new StaleWorkspaceError();
    if (appliesFinancialChange(config, response.data)) useAppStore.getState().resetAssistantContext();
    return response;
});
