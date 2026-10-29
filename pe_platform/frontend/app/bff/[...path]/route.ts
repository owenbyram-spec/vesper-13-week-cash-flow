import { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
const stripped = new Set(["connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"]);
function localBackendOrigin() {
    const value = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || "http://127.0.0.1:8771";
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
        throw new Error("BACKEND_URL must be a loopback API origin without credentials or a path.");
    }
    return url.origin;
}
async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
    let origin: string;
    try { origin = localBackendOrigin(); }
    catch { return Response.json({ detail: "Invalid local backend configuration." }, { status: 500 }); }
    const { path } = await context.params;
    if (path.some((part) => part === "." || part === ".." || part.includes("/") || part.includes("\\"))) {
        return Response.json({ detail: "Invalid API path." }, { status: 400 });
    }
    const target = `${origin}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
    const headers = new Headers(request.headers);
    for (const name of ["host", "content-length", "connection", "transfer-encoding", "accept-encoding", "authorization", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for"]) headers.delete(name);
    // Preserve the actual Origin, local session cookie, CSRF and workspace headers.
    const init: RequestInit & { duplex?: "half" } = { method: request.method, headers, redirect: "manual", signal: request.signal };
    if (!["GET", "HEAD"].includes(request.method)) { init.body = request.body; init.duplex = "half"; }
    let result: Response;
    try { result = await fetch(target, init); }
    catch { return Response.json({ detail: "The local backend is unavailable. Keep the launcher running." }, { status: 502 }); }
    const responseHeaders = new Headers();
    result.headers.forEach((value, name) => { if (!stripped.has(name.toLowerCase()) && name.toLowerCase() !== "set-cookie") responseHeaders.append(name, value); });
    for (const cookie of result.headers.getSetCookie()) responseHeaders.append("set-cookie", cookie);
    return new Response(result.body, { status: result.status, headers: responseHeaders });
}
export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const HEAD = proxy;
