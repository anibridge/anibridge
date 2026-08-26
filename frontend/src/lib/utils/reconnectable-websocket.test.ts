import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createReconnectableWebSocket } from "$lib/utils/reconnectable-websocket";

class MockWebSocket {
    static instances: MockWebSocket[] = [];

    readonly url: string;
    readonly close = vi.fn();
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;

    constructor(url: string | URL) {
        this.url = String(url);
        MockWebSocket.instances.push(this);
    }

    emitClose(): void {
        this.onclose?.({} as CloseEvent);
    }
}

beforeEach(() => {
    MockWebSocket.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("createReconnectableWebSocket", () => {
    it("reconnects after an unexpected close", () => {
        const connection = createReconnectableWebSocket("ws://example.test/status", {
            reconnectDelay: 1000,
        });
        connection.connect();

        MockWebSocket.instances[0].emitClose();
        vi.advanceTimersByTime(999);
        expect(MockWebSocket.instances).toHaveLength(1);

        vi.advanceTimersByTime(1);
        expect(MockWebSocket.instances).toHaveLength(2);
    });

    it("cancels pending reconnects when disconnected", () => {
        const connection = createReconnectableWebSocket("ws://example.test/status");
        connection.connect();
        MockWebSocket.instances[0].emitClose();

        connection.disconnect();
        vi.runAllTimers();

        expect(MockWebSocket.instances).toHaveLength(1);
    });

    it("closes only its current socket when disconnected", () => {
        const unrelated = new MockWebSocket("ws://example.test/unrelated");
        const connection = createReconnectableWebSocket("ws://example.test/status");
        connection.connect();
        const owned = MockWebSocket.instances[1];

        connection.disconnect();

        expect(owned.close).toHaveBeenCalledOnce();
        expect(unrelated.close).not.toHaveBeenCalled();
    });

    it("uses the latest URL when explicitly reconnected", () => {
        let channel = "first";
        const connection = createReconnectableWebSocket(
            () => `ws://example.test/${channel}`,
        );
        connection.connect();
        const first = MockWebSocket.instances[0];

        channel = "second";
        connection.connect();

        expect(first.close).toHaveBeenCalledOnce();
        expect(MockWebSocket.instances.map((socket) => socket.url)).toEqual([
            "ws://example.test/first",
            "ws://example.test/second",
        ]);
        first.emitClose();
        vi.runAllTimers();
        expect(MockWebSocket.instances).toHaveLength(2);
    });
});
