export interface ReconnectableWebSocketOptions {
    reconnectDelay?: number;
    onOpen?: (event: Event) => void;
    onMessage?: (event: MessageEvent) => void;
    onClose?: (event: CloseEvent) => void;
    onError?: (event: Event) => void;
}

export interface ReconnectableWebSocket {
    connect(): void;
    disconnect(): void;
}

export function createReconnectableWebSocket(
    url: string | (() => string),
    options: ReconnectableWebSocketOptions = {},
): ReconnectableWebSocket {
    const reconnectDelay = options.reconnectDelay ?? 2000;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let stopped = true;

    function clearReconnectTimer(): void {
        if (reconnectTimer === null) return;
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    function closeSocket(): void {
        const current = socket;
        socket = null;
        if (!current) return;

        current.onopen = null;
        current.onmessage = null;
        current.onclose = null;
        current.onerror = null;
        current.close();
    }

    function scheduleReconnect(): void {
        if (stopped || reconnectTimer !== null) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            openSocket();
        }, reconnectDelay);
    }

    function openSocket(): void {
        if (stopped) return;

        let current: WebSocket;
        try {
            current = new WebSocket(typeof url === "function" ? url() : url);
        } catch {
            scheduleReconnect();
            return;
        }

        socket = current;
        current.onopen = (event) => {
            if (socket !== current) return;
            options.onOpen?.(event);
        };
        current.onmessage = (event) => {
            if (socket !== current) return;
            options.onMessage?.(event);
        };
        current.onerror = (event) => {
            if (socket !== current) return;
            options.onError?.(event);
        };
        current.onclose = (event) => {
            if (socket !== current) return;
            socket = null;
            options.onClose?.(event);
            scheduleReconnect();
        };
    }

    return {
        connect(): void {
            stopped = false;
            clearReconnectTimer();
            closeSocket();
            openSocket();
        },
        disconnect(): void {
            stopped = true;
            clearReconnectTimer();
            closeSocket();
        },
    };
}
