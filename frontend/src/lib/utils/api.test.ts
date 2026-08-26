import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch, ApiHttpError, apiJson } from "$lib/utils/api";

vi.mock("$lib/utils/notify", () => ({ toast: vi.fn() }));

function stubResponse(response: Response): void {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
}

afterEach(() => {
    vi.unstubAllGlobals();
});

describe("apiJson", () => {
    it("throws a typed error with JSON response details", async () => {
        const data = { detail: "Configuration changed on disk" };
        stubResponse(
            new Response(JSON.stringify(data), {
                status: 409,
                headers: { "content-type": "application/json" },
            }),
        );

        const error = await apiJson("/api/config").catch((reason) => reason);

        expect(error).toBeInstanceOf(ApiHttpError);
        expect(error).toMatchObject({ message: data.detail, status: 409, data });
    });

    it("throws a typed error with a text response body", async () => {
        stubResponse(new Response("Service unavailable", { status: 503 }));

        const error = await apiJson("/api/status").catch((reason) => reason);

        expect(error).toBeInstanceOf(ApiHttpError);
        expect(error).toMatchObject({
            message: "Service unavailable",
            status: 503,
            data: "Service unavailable",
        });
    });

    it("returns successful JSON responses", async () => {
        const data = { profiles: {} };
        stubResponse(
            new Response(JSON.stringify(data), {
                status: 200,
                headers: { "content-type": "application/json" },
            }),
        );

        await expect(apiJson("/api/status")).resolves.toEqual(data);
    });
});

describe("apiFetch", () => {
    it("returns non-success responses for response-oriented callers", async () => {
        stubResponse(
            new Response(JSON.stringify({ detail: "Invalid request" }), {
                status: 422,
                headers: { "content-type": "application/json" },
            }),
        );

        const response = await apiFetch("/api/config");

        expect(response.status).toBe(422);
        expect(response.ok).toBe(false);
    });
});
