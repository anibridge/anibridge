<script lang="ts">
    import { onMount } from "svelte";

    import {
        Activity,
        ChevronsDown,
        Download,
        FolderSearch,
        Pause,
        RefreshCw,
        Search,
        TextAlignStart,
        TextWrap,
        Trash2,
        X,
    } from "@lucide/svelte";
    import { Tabs } from "bits-ui";

    import type { LogEntry, LogFile } from "$lib/types/api";
    import { apiFetch, buildWebSocketUrl } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    let tab = $state<"live" | "history">("live");
    let level = $state("DEBUG");
    let search = $state("");
    let wrap = $state(false);
    let autoScroll = $state(true);
    let logs: LogEntry[] = $state([]);
    let filtered: LogEntry[] = $state([]);
    let ws: WebSocket | null = null;
    let isWsOpen = $state(false);
    let lastReceived: number | null = $state(null);
    let files: LogFile[] = $state([]);
    let currentFile: LogFile | null = $state(null);
    let historyEntries: LogEntry[] = $state([]);
    let historyFiltered: LogEntry[] = $state([]);
    let historyLines = $state(500);
    let lastHistoryLinesLoaded: number | null = $state(null);
    let historyScroller: HTMLDivElement | null = $state(null);
    let liveScroller: HTMLDivElement | null = $state(null);
    let showFiles = $state(true);
    let isMobile = $state(false);

    const LEVEL_ORDER: Record<string, number> = {
        DEBUG: 10,
        INFO: 20,
        SUCCESS: 25,
        WARNING: 30,
        ERROR: 40,
    };

    function levelRank(l: string) {
        return LEVEL_ORDER[l] ?? 0;
    }

    function entryClass(l: string) {
        return (
            {
                DEBUG: "border-l-slate-700/60",
                INFO: "border-l-slate-600/60",
                SUCCESS: "border-l-emerald-600/70",
                WARNING: "border-l-amber-500/80",
                ERROR: "border-l-red-600/80",
            }[l] || "border-l-slate-700/60"
        );
    }

    function badgeClass(l: string) {
        return (
            {
                DEBUG: "bg-slate-800/70 text-slate-400",
                INFO: "bg-slate-800/70 text-slate-300",
                SUCCESS:
                    "bg-emerald-700/30 text-emerald-300 border border-emerald-700/40",
                WARNING: "bg-amber-700/30 text-amber-300 border border-amber-700/40",
                ERROR: "bg-red-700/30 text-red-300 border border-red-700/40",
            }[l] || "bg-slate-800/70 text-slate-300"
        );
    }

    function formatTime(e: LogEntry) {
        if (e.timestamp) {
            try {
                return new Date(e.timestamp).toLocaleTimeString([], { hour12: false });
            } catch {}
        }
        return "";
    }

    function formatLastReceived() {
        try {
            const diff = Date.now() - lastReceived!;
            if (diff < 1000) return "just now";
            if (diff < 60000) return Math.floor(diff / 1000) + "s ago";
            if (diff < 3600000) return Math.floor(diff / 60000) + "m ago";
            if (diff < 86400000) return Math.floor(diff / 3600000) + "h ago";
            return Math.floor(diff / 86400000) + "d ago";
        } catch {}
    }

    function applyFilter() {
        const minRank = levelRank(level);
        const q = search.toLowerCase();
        filtered = logs.filter(
            (l) =>
                levelRank(l.level) >= minRank &&
                (!q || (l.message || "").toLowerCase().includes(q)),
        );
        historyFiltered = historyEntries.filter(
            (l) =>
                levelRank(l.level) >= minRank &&
                (!q || (l.message || "").toLowerCase().includes(q)),
        );
    }

    function scrollToBottom(which: "live" | "history") {
        const el = which === "live" ? liveScroller : historyScroller;
        if (el) el.scrollTop = el.scrollHeight;
    }

    function openWs() {
        try {
            ws?.close();
        } catch {}
        ws = new WebSocket(buildWebSocketUrl("/ws/logs"));
        ws.onopen = () => {
            isWsOpen = true;
        };
        ws.onmessage = (ev) => {
            try {
                const d = JSON.parse(ev.data);
                logs.push(d);
                lastReceived = Date.now();
                applyFilter();
                if (autoScroll && tab === "live") scrollToBottom("live");
            } catch {}
        };
        ws.onclose = () => {
            isWsOpen = false;
            setTimeout(openWs, 2000);
        };
    }

    async function refreshFiles() {
        try {
            const r = await apiFetch("/api/logs/files", undefined, { silent: true });
            if (!r.ok) return;
            files = await r.json();
            if (!currentFile) {
                const active = files.find((f) => f.current);
                if (active) loadFile(active);
            }
        } catch {
            toast("Failed to load log files", "error");
        }
    }

    async function loadFile(f: LogFile, force = false) {
        const same = currentFile && currentFile.name === f.name;
        if (!force && same && lastHistoryLinesLoaded === historyLines) return;
        currentFile = f;
        try {
            const r = await apiFetch(
                `/api/logs/file/${encodeURIComponent(f.name)}?lines=${historyLines}`,
                undefined,
                { silent: true },
            );
            if (!r.ok) return;
            historyEntries = await r.json();
            lastHistoryLinesLoaded = historyLines;
            applyFilter();
            requestAnimationFrame(() => {
                if (historyLines > 0) {
                    scrollToBottom("history");
                    return;
                }
                if (historyScroller) historyScroller.scrollTop = 0;
            });
        } catch {
            toast("Failed to load log file", "error");
        }
    }

    function downloadLive() {
        if (!logs.length) return;
        const blob = new Blob(
            [logs.map((l) => `[${l.level}] ${l.message}`).join("\n")],
            { type: "text/plain" },
        );
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download =
            "logs-" +
            new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19) +
            ".txt";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            URL.revokeObjectURL(a.href);
            a.remove();
        }, 100);
    }

    function downloadHistory() {
        if (!historyEntries.length) return;
        const blob = new Blob(
            [historyEntries.map((l) => `[${l.level}] ${l.message}`).join("\n")],
            { type: "text/plain" },
        );
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download =
            (currentFile ? currentFile.name.replace(/\.log.*/, "") : "history") +
            "-excerpt-" +
            new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19) +
            ".txt";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            URL.revokeObjectURL(a.href);
            a.remove();
        }, 100);
    }

    function clearLive() {
        logs = [];
        filtered = [];
    }

    function formatFileTime(ms: number) {
        try {
            return new Date(ms).toLocaleString();
        } catch {
            return "";
        }
    }

    function formatSize(bytes: number) {
        if (bytes < 1024) return bytes + " B";
        const units = ["KB", "MB", "GB"];
        let v = bytes / 1024,
            i = 0;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i++;
        }
        return v.toFixed(v < 10 ? 1 : 0) + " " + units[i];
    }
    function highlightLog(msg: string) {
        if (msg == null) return "";
        let safe = String(msg)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        // $$'text'$$ => highlighted (light up)
        safe = safe.replace(
            /\$\$'((?:[^']|'(?!\$\$))*)'\$\$/g,
            (_m, inner) => `<span class="text-sky-300 font-semibold">'${inner}'</span>`,
        );

        // $${...}$$ => greyed out
        safe = safe.replace(
            /\$\$\{((?:[^}]|\}(?!\$\$))*)\}\$\$/g,
            (_m, inner) => `<span class="text-slate-500">{${inner}}</span>`,
        );

        return safe;
    }

    function switchTab(t: "live" | "history") {
        tab = t;
        if (t === "live" && autoScroll)
            requestAnimationFrame(() => scrollToBottom("live"));
    }

    function persistPrefs() {
        try {
            localStorage.setItem(
                "logs.prefs",
                JSON.stringify({ level, autoScroll, wrap }),
            );
            localStorage.setItem("logs.tab", tab);
        } catch {}
    }

    function loadPrefs() {
        try {
            const p = JSON.parse(localStorage.getItem("logs.prefs") || "null");
            if (p) {
                level = p.level || level;
                autoScroll = !!p.autoScroll;
                wrap = !!p.wrap;
            }
            const t = localStorage.getItem("logs.tab");
            if (t === "live" || t === "history") tab = t;
        } catch {}
    }

    onMount(() => {
        loadPrefs();
        openWs();
        refreshFiles();
        const updateIsMobile = () => {
            isMobile = window.innerWidth < 640; // Tailwind sm breakpoint
            if (isMobile && tab === "history" && showFiles) {
                showFiles = false;
            }
        };
        updateIsMobile();
        window.addEventListener("resize", updateIsMobile);
        return () => window.removeEventListener("resize", updateIsMobile);
    });

    $effect(() => {
        applyFilter();
        persistPrefs();
    });
</script>

<div class="space-y-6">
    <!-- Toolbar -->
    <div class="space-y-2 border-b border-slate-800/70 py-2 text-sm font-medium">
        <!-- Tabs -->
        <Tabs.Root
            value={tab}
            onValueChange={(v) => switchTab(v as typeof tab)}
            class="flex items-center gap-2">
            <Tabs.List class="flex items-center gap-2">
                <Tabs.Trigger
                    value="live"
                    class="inline-flex h-9 items-center gap-1 rounded-md px-4 text-xs font-medium text-slate-400 hover:text-slate-200 data-[state=active]:bg-slate-800/80 data-[state=active]:text-slate-100">
                    <Activity class="inline h-4 w-4 text-[13px]" /> Live
                    <span
                        class="ml-1 h-1.5 w-1.5 rounded-full"
                        class:bg-emerald-400={isWsOpen}
                        class:bg-amber-400={!isWsOpen}></span>
                </Tabs.Trigger>
                <Tabs.Trigger
                    value="history"
                    class="inline-flex h-9 items-center gap-1 rounded-md px-4 text-xs font-medium text-slate-400 hover:text-slate-200 data-[state=active]:bg-slate-800/80 data-[state=active]:text-slate-100">
                    <FolderSearch class="inline h-4 w-4 text-[13px]" /> History
                </Tabs.Trigger>
            </Tabs.List>
        </Tabs.Root>
        <div class="flex flex-wrap items-center justify-between gap-2">
            <!-- Log Level + Search -->
            <div class="flex w-full items-center gap-2">
                <div>
                    <label
                        for="log-level"
                        class="sr-only">Min level</label>
                    <select
                        id="log-level"
                        bind:value={level}
                        onchange={applyFilter}
                        class="h-8 rounded-md border border-slate-700/70 bg-slate-900/70 pl-2 text-[11px] shadow-sm focus:border-slate-600 focus:bg-slate-900">
                        <option>DEBUG</option><option>INFO</option><option
                            >SUCCESS</option
                        ><option>WARNING</option><option>ERROR</option>
                    </select>
                </div>
                <div class="relative w-full">
                    <label
                        for="log-search"
                        class="sr-only">Search</label>
                    <input
                        id="log-search"
                        bind:value={search}
                        oninput={() => applyFilter()}
                        placeholder="Search..."
                        class="h-8 w-full rounded-md border border-slate-700/70 bg-slate-900/70 pr-8 pl-8 text-[11px] shadow-sm placeholder:text-slate-500 focus:border-slate-600 focus:bg-slate-900" />
                    <Search
                        class="absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                    {#if search}
                        <button
                            aria-label="Clear search"
                            type="button"
                            onclick={() => ((search = ""), applyFilter())}
                            class="absolute top-1/2 right-1 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            <X class="h-3.5 w-3.5 text-[14px]" />
                        </button>
                    {/if}
                </div>
                <!-- History files toggle (mobile) -->
                {#if tab === "history" && isMobile}
                    <div class="flex flex-wrap items-center gap-2">
                        <button
                            type="button"
                            aria-label="Toggle files sidebar"
                            title={showFiles ? "Hide files list" : "Show files list"}
                            onclick={() => (showFiles = !showFiles)}
                            class={`inline-flex h-8 w-8 items-center justify-center rounded-md ring-1 ring-slate-700/60 transition-colors ${showFiles ? "bg-amber-600 text-white hover:bg-amber-500" : "bg-slate-800 text-amber-300 hover:bg-slate-700"}`}>
                            <FolderSearch class="inline h-4 w-4" />
                        </button>
                    </div>
                {/if}
            </div>
        </div>
    </div>
    <div
        class="relative flex h-[75vh] flex-col overflow-hidden rounded-md border border-slate-800/70 bg-slate-900/40 backdrop-blur-sm">
        <!-- Live Tab -->
        {#if tab === "live"}
            <div class="flex h-full flex-col">
                <div
                    class="flex items-center justify-between gap-3 border-b border-slate-800/60 bg-slate-950/60 px-3 py-2 text-[11px]">
                    <div class="flex items-center gap-3">
                        <span class="font-medium text-slate-400">Live stream</span>
                        <span class="text-slate-500"
                            >{filtered.length}/{logs.length} shown</span>
                        <span
                            class="h-1.5 w-1.5 rounded-md"
                            class:bg-emerald-400={isWsOpen}
                            class:bg-amber-400={!isWsOpen}
                            title={isWsOpen ? "Connected" : "Reconnecting..."}></span>
                        {#if lastReceived}
                            <span class="hidden text-slate-500 md:inline"
                                >Updated {formatLastReceived()}</span>
                        {/if}
                    </div>
                    <div class="flex items-center gap-2">
                        <button
                            type="button"
                            aria-label="Clear live logs"
                            title="Clear live logs"
                            onclick={clearLive}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            <Trash2 class="inline h-4 w-4" />
                        </button>
                        <button
                            type="button"
                            aria-label="Toggle auto scroll"
                            title={autoScroll
                                ? "Auto-scroll enabled"
                                : "Auto-scroll paused"}
                            onclick={() => ((autoScroll = !autoScroll), persistPrefs())}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            {#if autoScroll}
                                <ChevronsDown class="inline h-4 w-4" />
                            {:else}
                                <Pause class="inline h-4 w-4" />
                            {/if}
                        </button>
                        <button
                            type="button"
                            aria-label="Toggle wrap"
                            title={wrap ? "Disable wrap" : "Enable wrap"}
                            onclick={() => ((wrap = !wrap), persistPrefs())}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            {#if wrap}
                                <TextWrap class="inline h-4 w-4" />
                            {:else}
                                <TextAlignStart class="inline h-4 w-4" />
                            {/if}
                        </button>
                        <button
                            type="button"
                            aria-label="Download live logs"
                            title="Download live logs"
                            onclick={downloadLive}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            <Download class="inline h-4 w-4" />
                        </button>
                    </div>
                </div>
                <div
                    bind:this={liveScroller}
                    class="scrollbar-thin flex-1 overflow-y-auto p-1 font-mono text-[11px] leading-normal"
                    class:overflow-x-auto={!wrap}
                    class:overflow-x-hidden={wrap}
                    style="touch-action: auto;">
                    <div
                        class="min-w-full"
                        style:width={wrap ? "auto" : "max-content"}>
                        {#each filtered as entry, i (`${entry.timestamp}-${i}`)}
                            <div
                                class={`group flex items-start gap-2 border-l-2 px-2 py-0.5 pr-3 ${entryClass(entry.level)}`}>
                                <span
                                    class="hidden w-13.5 shrink-0 text-right text-[10px] text-slate-500 tabular-nums sm:inline-block">
                                    {formatTime(entry)}
                                </span>
                                <span
                                    class={`flex h-5 shrink-0 items-center rounded-md bg-slate-800/70 px-1 text-[10px] font-semibold tracking-wide text-slate-300 ${badgeClass(entry.level)}`}
                                    >{entry.level}</span>
                                <div
                                    class="min-w-0 flex-1 text-slate-200"
                                    class:overflow-x-auto={!wrap}
                                    class:overflow-x-hidden={wrap}
                                    class:whitespace-pre-wrap={wrap}
                                    class:whitespace-pre={!wrap}
                                    style="-webkit-overflow-scrolling: touch; word-break: normal;"
                                    style:overflow-wrap={wrap ? "anywhere" : "normal"}>
                                    <div
                                        class:inline-block={!wrap}
                                        class:whitespace-pre={!wrap}>
                                        <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                                        {@html highlightLog(entry.message)}
                                    </div>
                                </div>
                                <span class="text-[10px] text-slate-500 sm:hidden">
                                    {formatTime(entry)}
                                </span>
                            </div>
                        {/each}
                        {#if !filtered.length}<p class="p-2 text-xs text-slate-500">
                                No log entries.
                            </p>{/if}
                    </div>
                </div>
            </div>
        {/if}
        <!-- History Tab -->
        {#if tab === "history"}
            <div class="flex h-full flex-col">
                <div
                    class="flex items-center justify-between gap-3 border-b border-slate-800/60 bg-slate-950/60 px-3 py-2 text-[11px]">
                    <div class="flex items-center gap-2">
                        <span class="text-slate-400"
                            >{currentFile ? currentFile.name : "Select a file"}</span>
                        {#if historyEntries.length}<span
                                class="hidden text-slate-500 sm:inline"
                                >({historyEntries.length} lines)</span
                            >{/if}
                    </div>
                    <div class="flex items-center gap-2">
                        <label
                            for="lines"
                            class="sr-only">Lines</label>
                        <select
                            id="lines"
                            bind:value={historyLines}
                            onchange={() => currentFile && loadFile(currentFile)}
                            class="h-7 rounded-md border border-slate-700/60 bg-slate-900/70 px-1 text-[11px]">
                            <option value={0}>All lines</option>
                            {#each [100, 250, 500, 1000, 2000] as n (n)}
                                <option value={n}>{n} lines</option>
                            {/each}
                        </select>
                        <button
                            type="button"
                            aria-label="Toggle wrap"
                            title={wrap ? "Disable wrap" : "Enable wrap"}
                            onclick={() => ((wrap = !wrap), persistPrefs())}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700">
                            {#if wrap}
                                <TextWrap class="inline h-4 w-4" />
                            {:else}
                                <TextAlignStart class="inline h-4 w-4" />
                            {/if}
                        </button>
                        <button
                            type="button"
                            aria-label="Refresh"
                            onclick={() => currentFile && loadFile(currentFile, true)}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700"
                            ><RefreshCw class="inline h-4 w-4" /></button>
                        <button
                            type="button"
                            aria-label="Download file excerpt"
                            disabled={!historyEntries.length}
                            onclick={downloadHistory}
                            class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700 disabled:opacity-40"
                            ><Download class="inline h-4 w-4" /></button>
                    </div>
                </div>
                <div class="flex min-w-0 flex-1 overflow-hidden">
                    <div
                        class={`flex w-64 flex-col border-r border-slate-800/60 transition-transform duration-300 ease-out md:translate-x-0 ${isMobile ? "absolute inset-y-0 left-0 z-30 bg-slate-950/95 backdrop-blur-sm" : ""}`}
                        class:-translate-x-full={!showFiles}>
                        <div
                            class="border-b border-slate-800/60 px-3 py-1 text-[10px] text-slate-500">
                            <div class="flex items-center gap-2">
                                <span
                                    >{files.length} file{files.length === 1
                                        ? ""
                                        : "s"}</span>
                                {#if isMobile}
                                    <button
                                        type="button"
                                        aria-label="Close file list"
                                        class="ml-auto inline-flex h-6 w-6 items-center justify-center rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700 sm:hidden"
                                        onclick={() => (showFiles = false)}>
                                        <X class="h-3.5 w-3.5" />
                                    </button>
                                {/if}
                            </div>
                        </div>
                        <div class="flex-1 divide-y divide-slate-800/60 overflow-auto">
                            {#each files as f (f.name)}
                                <button
                                    type="button"
                                    onclick={() => loadFile(f)}
                                    class={`group flex w-full flex-col gap-0.5 px-3 py-2 text-left text-[11px] hover:bg-slate-800/70 ${currentFile && currentFile.name === f.name ? "bg-slate-800 ring-1 ring-emerald-600/40 ring-inset" : ""}`}>
                                    <div
                                        class="flex items-center justify-between gap-2">
                                        <span
                                            class="truncate font-medium text-slate-200"
                                            >{f.name}</span>
                                        <span
                                            class={`rounded-md px-1 text-[10px] ${f.current ? "border border-emerald-700/40 bg-emerald-700/30 text-emerald-300" : "bg-slate-800/70 text-slate-400"}`}
                                            >{f.current ? "active" : "archived"}</span>
                                    </div>
                                    <div
                                        class="flex items-center gap-2 text-[10px] text-slate-500">
                                        <span>{formatFileTime(f.mtime)}</span><span
                                            >•</span
                                        ><span>{formatSize(f.size)}</span>
                                    </div>
                                </button>
                            {/each}
                            {#if !files.length}<p
                                    class="p-3 text-[11px] text-slate-500">
                                    No log files found.
                                </p>{/if}
                        </div>
                    </div>
                    {#if isMobile && showFiles}
                        <div
                            class="fixed inset-0 z-20 bg-black/50 md:hidden"
                            onclick={() => (showFiles = false)}
                            aria-hidden="true">
                        </div>
                    {/if}
                    <div class="flex min-w-0 flex-1 flex-col">
                        <div
                            bind:this={historyScroller}
                            class="scrollbar-thin min-w-0 flex-1 overflow-y-auto p-1 font-mono text-[11px] leading-normal"
                            class:overflow-x-auto={!wrap}
                            class:overflow-x-hidden={wrap}
                            style="touch-action: auto;">
                            <div
                                class="min-w-full"
                                style:width={wrap ? "auto" : "max-content"}>
                                {#each historyFiltered as entry, i (`${entry.timestamp}-${i}`)}
                                    <div
                                        class={`group flex items-start gap-2 border-l-2 px-2 py-0.5 pr-3 ${entryClass(entry.level)}`}>
                                        <span
                                            class="hidden w-13.5 shrink-0 text-right text-[10px] text-slate-500 tabular-nums sm:inline-block">
                                            {formatTime(entry)}
                                        </span>
                                        <span
                                            class={`flex h-5 shrink-0 items-center rounded-md bg-slate-800/70 px-1 text-[10px] font-semibold tracking-wide text-slate-300 ${badgeClass(entry.level)}`}
                                            >{entry.level}</span>
                                        <div
                                            class="min-w-0 flex-1 text-slate-200"
                                            class:overflow-x-auto={!wrap}
                                            class:overflow-x-hidden={wrap}
                                            class:whitespace-pre-wrap={wrap}
                                            class:whitespace-pre={!wrap}
                                            style="-webkit-overflow-scrolling: touch; word-break: normal;"
                                            style:overflow-wrap={wrap
                                                ? "anywhere"
                                                : "normal"}>
                                            <div
                                                class:inline-block={!wrap}
                                                class:whitespace-pre={!wrap}>
                                                <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                                                {@html highlightLog(entry.message)}
                                            </div>
                                        </div>
                                        <span
                                            class="text-[10px] text-slate-500 sm:hidden">
                                            {formatTime(entry)}
                                        </span>
                                    </div>
                                {/each}
                                {#if currentFile && !historyFiltered.length}<p
                                        class="p-2 text-xs text-slate-500">
                                        No lines match.
                                    </p>{/if}
                                {#if !currentFile}<p class="p-2 text-xs text-slate-500">
                                        Select a file to view its tail.
                                    </p>{/if}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        {/if}
    </div>
</div>
