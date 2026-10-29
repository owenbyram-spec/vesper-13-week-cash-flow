import { afterEach, describe, expect, it, vi } from "vitest";
import configuration from "../next.config.mjs";
afterEach(() => vi.unstubAllEnvs());
describe("local platform response privacy", () => {
    it("blocks model-supplied remote images and browser connections in production", async () => {
        vi.stubEnv("NODE_ENV", "production");
        const routes = await configuration.headers!();
        const headers = Object.fromEntries(routes[0].headers.map(({ key, value }) => [key, value]));
        const policy = headers["Content-Security-Policy"];
        expect(policy).toContain("img-src 'self' data:");
        expect(policy).toContain("connect-src 'self'");
        expect(policy).toContain("object-src 'none'");
        expect(policy).toContain("frame-ancestors 'none'");
        expect(policy).not.toContain("https:");
        expect(policy).not.toContain("ws://");
        expect(policy).not.toContain("unsafe-eval");
        expect(headers["Referrer-Policy"]).toBe("no-referrer");
        expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    });
    it("allows only loopback HMR connections in development", async () => {
        vi.stubEnv("NODE_ENV", "development");
        const policy = (await configuration.headers!())[0].headers.find(({ key }) => key === "Content-Security-Policy")!.value;
        expect(policy).toContain("ws://127.0.0.1:*");
        expect(policy).toContain("ws://localhost:*");
        expect(policy).toContain("img-src 'self' data:");
        expect(policy).not.toContain("connect-src *");
    });
});
