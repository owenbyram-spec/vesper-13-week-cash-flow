import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST } from "./route";

beforeEach(() => { vi.stubEnv("BACKEND_URL", "http://127.0.0.1:8771"); });
afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });
describe("pinned local streaming proxy", () => {
    it("preserves actual Origin, session, CSRF and displayed workspace while stripping forwarded authority", async () => {
        const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("fixture response", { status: 200, headers: { "content-type": "text/event-stream", "set-cookie": "fixture-session=fixture; HttpOnly; SameSite=Strict" } }));
        const request = new NextRequest("http://127.0.0.1:8770/bff/cashflow-forecast/override", { method: "POST", body: "{}", headers: { Origin: "http://127.0.0.1:8770", Cookie: "fixture-session=fixture", "X-CSRF-Token": "fixture-csrf", "X-Vesper-Workspace": "workspace-a", "X-Vesper-Revision": "3", Authorization: "fixture-untrusted", "X-Forwarded-Host": "untrusted.example" } });
        const response = await POST(request, { params: Promise.resolve({ path: ["cashflow-forecast", "override"] }) });
        expect(fetch.mock.calls[0][0]).toBe("http://127.0.0.1:8771/cashflow-forecast/override");
        const headers = fetch.mock.calls[0][1]?.headers as Headers;
        expect(headers.get("origin")).toBe("http://127.0.0.1:8770");
        expect(headers.get("cookie")).toBe("fixture-session=fixture");
        expect(headers.get("x-csrf-token")).toBe("fixture-csrf");
        expect(headers.get("x-vesper-workspace")).toBe("workspace-a");
        expect(headers.get("x-vesper-revision")).toBe("3");
        expect(headers.get("authorization")).toBeNull();
        expect(headers.get("x-forwarded-host")).toBeNull();
        expect(response.headers.get("set-cookie")).toContain("HttpOnly");
        expect(await response.text()).toBe("fixture response");
    });
    it("refuses a non-loopback backend before opening a network request", async () => {
        vi.stubEnv("BACKEND_URL", "https://untrusted.example");
        const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected request"));
        const response = await POST(new NextRequest("http://127.0.0.1:8770/bff/local/bootstrap", { method: "POST" }), { params: Promise.resolve({ path: ["local", "bootstrap"] }) });
        expect(response.status).toBe(500);
        expect(fetch).not.toHaveBeenCalled();
    });
    it("refuses a traversal segment before forwarding", async () => {
        const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected request"));
        const response = await POST(new NextRequest("http://127.0.0.1:8770/bff/local/bootstrap", { method: "POST" }), { params: Promise.resolve({ path: ["..", "bootstrap"] }) });
        expect(response.status).toBe(400);
        expect(fetch).not.toHaveBeenCalled();
    });
});
