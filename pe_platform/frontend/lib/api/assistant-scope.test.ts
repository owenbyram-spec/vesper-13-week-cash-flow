import { beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { streamAssistantMessage, formatAssistantError, assistantMessageForStatus, type AssistantStreamEvent } from "./assistant";
import { apiClient, setCsrfFallbackToken } from "./client";
import { useAppStore } from "../store";
import { StaleWorkspaceError } from "../workspace-scope";
function adopt(id = "a", revision = 1) {
    useAppStore.getState().adoptBootstrap({ workspace: { id, revision, realm_id: `realm-${id}`, organization_id: "local", name: id, currency: "USD", synthetic: true }, workspaces: [], csrf_token: "fixture-csrf", data_directory: "/fixture", source_revision: "fixture" });
}
beforeEach(() => { adopt(); setCsrfFallbackToken("fixture-csrf"); });
describe("real assistant transport scope adapter", () => {
    it("uses Vesper Bot identity in connection and permission errors", () => {
        for (const status of [401, 403, undefined]) {
            expect(assistantMessageForStatus(status)).toContain("Vesper Bot");
        }
    });

    it("aborts and suppresses old-workspace events without fallback after selection changes", async () => {
        let controller!: ReadableStreamDefaultController<Uint8Array>;
        const encoder = new TextEncoder();
        const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
        const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(stream, { status: 200 }));
        const fallback = vi.spyOn(apiClient, "post").mockRejectedValue(new Error("Unexpected fallback"));
        const events: AssistantStreamEvent[] = [];
        try {
            const request = streamAssistantMessage({ message: "Explain cash", realm_id: "realm-a" }, (event) => events.push(event));
            const rejected = expect(request).rejects.toBeInstanceOf(StaleWorkspaceError);
            controller.enqueue(encoder.encode('data: {"type":"text","content":"Authorised A"}\n\n'));
            await waitFor(() => expect(events).toHaveLength(1));
            adopt("b", 2);
            expect((fetch.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true);
            controller.enqueue(encoder.encode('data: {"type":"text","content":"Late A"}\n\n'));
            controller.close();
            await rejected;
            expect(events).toEqual([{ type: "text", content: "Authorised A" }]);
            expect(fallback).not.toHaveBeenCalled();
        } finally { fetch.mockRestore(); fallback.mockRestore(); }
    });
    it("shows the local missing-connection detail instead of inventing a transport outage", async () => {
        const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "Save an AI connection in this workspace first." }), { status: 409, headers: { "Content-Type": "application/json" } }));
        try {
            let failure: unknown;
            await streamAssistantMessage({ message: "Explain", realm_id: "realm-a" }, () => {}).catch((error) => { failure = error; });
            expect(formatAssistantError(failure)).toBe("Save an AI connection in this workspace first.");
            expect(fetch).toHaveBeenCalledOnce();
        } finally { fetch.mockRestore(); }
    });
    it("refuses another company's payload before any provider request is started", async () => {
        const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected request"));
        try {
            await expect(streamAssistantMessage({ message: "Explain", realm_id: "realm-b" }, () => {})).rejects.toBeInstanceOf(StaleWorkspaceError);
            expect(fetch).not.toHaveBeenCalled();
        } finally { fetch.mockRestore(); }
    });
});
