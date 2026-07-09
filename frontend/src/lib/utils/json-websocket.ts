import { buildWebSocketUrl } from "$lib/utils/api";

type WebSocketPath = string | (() => string | null | undefined);

interface JsonWebSocketOptions<T> {
    path: WebSocketPath;
    reconnectMs?: number;
    onMessage?: (data: T, event: MessageEvent<string>) => void;
    onOpen?: (event: Event) => void;
    onClose?: (event: CloseEvent) => void;
    onError?: (event: Event) => void;
}

export interface JsonWebSocketHandle {
    connect: () => void;
    reconnect: () => void;
    close: () => void;
    socket: () => WebSocket | null;
}

function resolvePath(path: WebSocketPath): string | null {
    const resolved = typeof path === "function" ? path() : path;
    return resolved || null;
}

export function createJsonWebSocket<T = unknown>(
    options: JsonWebSocketOptions<T>,
): JsonWebSocketHandle {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = true;
    let generation = 0;

    function clearReconnect() {
        if (!reconnectTimer) return;
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    function closeSocket() {
        const current = socket;
        socket = null;
        try {
            current?.close();
        } catch {}
    }

    function connect() {
        closed = false;
        clearReconnect();
        generation += 1;
        const currentGeneration = generation;
        closeSocket();

        const path = resolvePath(options.path);
        if (!path) return;

        const nextSocket = new WebSocket(buildWebSocketUrl(path));
        socket = nextSocket;

        nextSocket.onopen = (event) => options.onOpen?.(event);
        nextSocket.onerror = (event) => options.onError?.(event);
        nextSocket.onmessage = (event: MessageEvent<string>) => {
            try {
                options.onMessage?.(JSON.parse(event.data) as T, event);
            } catch {}
        };
        nextSocket.onclose = (event) => {
            if (currentGeneration !== generation) return;
            socket = null;
            options.onClose?.(event);
            if (closed) return;
            reconnectTimer = setTimeout(connect, options.reconnectMs ?? 2000);
        };
    }

    function close() {
        closed = true;
        generation += 1;
        clearReconnect();
        closeSocket();
    }

    return { connect, reconnect: connect, close, socket: () => socket };
}
