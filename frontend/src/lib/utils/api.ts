import { toast, type ToastType } from "$lib/utils/notify";

function normalizePathPrefix(pathPrefix: string | null | undefined): string {
    if (!pathPrefix) return "";
    const trimmed = pathPrefix.trim();
    if (!trimmed || trimmed === "/") return "";
    return `/${trimmed.replace(/^\/+/, "").replace(/\/+$/, "")}`;
}

export function getPathPrefix(): string {
    if (typeof window === "undefined") return "";
    return normalizePathPrefix(window.__ANIBRIDGE_PATH_PREFIX);
}

export function buildAppPath(path: string): string {
    if (!path.startsWith("/") || path.startsWith("//")) return path;
    const pathPrefix = getPathPrefix();
    return pathPrefix ? `${pathPrefix}${path}` : path;
}

export function buildWebSocketUrl(path: string): string {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}${buildAppPath(path)}`;
}

function resolveRequestInput(input: RequestInfo | URL): RequestInfo | URL {
    if (typeof input === "string") {
        return buildAppPath(input);
    }

    if (
        input instanceof URL &&
        typeof window !== "undefined" &&
        input.origin === window.location.origin
    ) {
        const prefixedPath = buildAppPath(input.pathname);
        if (prefixedPath !== input.pathname) {
            const url = new URL(input.toString());
            url.pathname = prefixedPath;
            return url;
        }
    }

    return input;
}

export function isAbortError(error: unknown): boolean {
    if (!error) return false;
    if (error instanceof DOMException && error.name === "AbortError") return true;
    return (error as { name?: string }).name === "AbortError";
}

export interface ApiErrorData {
    message?: string;
    error?: string;
    detail?: string | { message?: string } | unknown;
}

function extractMessage(data: ApiErrorData, status: number): string {
    if (!data) return `Request failed (${status})`;
    if (data.message) return data.message;
    if (typeof data.detail === "string") return data.detail;
    if (data.error) return data.error;
    if (data.detail && typeof data.detail === "object" && "message" in data.detail) {
        // @ts-expect-error best attempt
        return data.detail.message || `Request failed (${status})`;
    }
    return `Request failed (${status})`;
}

export interface ApiOptions {
    silent?: boolean;
    successMessage?: string;
    successType?: ToastType;
}

export async function apiFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
    opts: ApiOptions = {},
): Promise<Response> {
    const resolvedInput = resolveRequestInput(input);
    let res: Response;
    try {
        res = await fetch(resolvedInput, init);
    } catch (e) {
        if (isAbortError(e)) throw e;
        if (!opts.silent) toast(`Network error: ${(e as Error).message || e}`, "error");
        throw e;
    }
    if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
            try {
                const data = (await res.clone().json()) as ApiErrorData;
                msg = extractMessage(data, res.status);
            } catch {}
        } else {
            try {
                const text = await res.clone().text();
                if (text.trim()) msg = text.slice(0, 300);
            } catch {}
        }
        if (!opts.silent) toast(msg, "error");
    } else if (opts.successMessage) {
        toast(opts.successMessage, opts.successType || "success");
    }
    return res;
}

export async function apiJson<T = unknown>(
    input: RequestInfo | URL,
    init?: RequestInit,
): Promise<T> {
    const r = await apiFetch(input, init);
    // If backend returned error body but still non-ok, apiFetch already toasted; allow caller to decide what to do.
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
        const text = await r.text();
        return JSON.parse(text) as T; // may throw which is fine
    }
    return (await r.json()) as T;
}
