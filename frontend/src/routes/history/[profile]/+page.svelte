<script lang="ts">
    import { onMount } from "svelte";
    import type { Component } from "svelte";

    import {
        CircleSlash,
        CloudDownload,
        History,
        LoaderCircle,
        Pin,
        RefreshCcw,
        RotateCw,
        Wrench,
    } from "@lucide/svelte";
    import { Meter } from "bits-ui";
    import { SvelteURLSearchParams } from "svelte/reactivity";
    import { fade } from "svelte/transition";

    import { page } from "$app/state";
    import TimelineGroupCard from "$lib/components/timeline/timeline-group-card.svelte";
    import TimelineOutcomeFilters from "$lib/components/timeline/timeline-outcome-filters.svelte";
    import { OUTCOME_META, type OutcomeMeta } from "$lib/components/timeline/types";
    import type {
        GetHistoryResponse,
        HistoryGroup,
        HistoryOperation,
        ProfileStatus,
        StatusResponse,
    } from "$lib/types/api";
    import { apiFetch, buildWebSocketUrl, isAbortError } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";
    import {
        progressCount,
        progressPercent,
        progressStage,
        progressSubject,
    } from "$lib/utils/sync-progress";

    const profile = $derived(page.params.profile ?? "");

    let groups: HistoryGroup[] = $state([]);
    let stats: Record<string, number> = $state({});
    let profiles: StatusResponse["profiles"] = $state({});
    let loading = $state(true);
    let loadingOlder = $state(false);
    let refreshing = $state(false);
    let acting = $state(false);
    let hasMore = $state(false);
    let nextBeforeId: number | null = $state(null);
    let latestGroupId: number | null = $state(null);
    let activeOutcome: string | null = $state(null);
    let lastRefreshed: number | null = $state(null);
    let pinManagerOpen = $state(false);
    let PinManagerComponent: Component<{
        profile: string;
        onClose: () => void;
        onChanged: () => void;
    }> | null = $state(null);

    let currentAbort: AbortController | null = null;
    let statusWs: WebSocket | null = null;
    let historyWs: WebSocket | null = null;
    let statusReconnect: ReturnType<typeof setTimeout> | null = null;
    let historyReconnect: ReturnType<typeof setTimeout> | null = null;
    let mounted = false;
    let destroyed = false;
    let activeProfile = "";

    const profileStatus = $derived<ProfileStatus | null>(profiles[profile] ?? null);
    const currentSync = $derived(profileStatus?.status?.current_sync ?? null);
    const isProfileRunning = $derived(currentSync?.state === "running");
    const outcomeFilterMeta = $derived(buildOutcomeFilterMeta(stats));

    const hasRunningSync = () => currentSync?.state === "running";
    const percent = () => progressPercent(currentSync) ?? 0;
    const isDeterminate = () => progressPercent(currentSync) !== null;

    function buildOutcomeFilterMeta(sourceStats: Record<string, number>) {
        const entries = [
            ...Object.keys(OUTCOME_META),
            ...Object.keys(sourceStats),
        ].filter((key, index, keys) => keys.indexOf(key) === index);
        return Object.fromEntries(
            entries
                .map(
                    (key) =>
                        [key, OUTCOME_META[key] ?? fallbackOutcomeMeta(key)] as const,
                )
                .sort((a, b) => a[1].order - b[1].order),
        );
    }

    function fallbackOutcomeMeta(key: string): OutcomeMeta {
        return { ...OUTCOME_META.skipped, label: key.replaceAll("_", " "), order: 900 };
    }

    function historyPath() {
        return `/api/history/${encodeURIComponent(profile)}`;
    }

    function pinPath(key: string) {
        return `/api/pins/${encodeURIComponent(profile)}/${encodeURIComponent(key)}`;
    }

    function historyParams(includeStats = true) {
        const params = new SvelteURLSearchParams({ limit: "25" });
        if (includeStats) params.set("include_stats", "true");
        if (activeOutcome) params.set("outcome", activeOutcome);
        return params;
    }

    function mergeGroups(nextGroups: HistoryGroup[], existing: HistoryGroup[]) {
        const merged: HistoryGroup[] = [];
        for (const group of [...nextGroups, ...existing]) {
            if (!merged.some((item) => item.id === group.id)) merged.push(group);
        }
        return merged;
    }

    function formatTimeAgo(ts: number | null) {
        if (!ts) return "never";
        const seconds = Math.floor((Date.now() - ts) / 1000);
        if (seconds < 45) return "just now";
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        return `${Math.floor(hours / 24)}d ago`;
    }

    async function loadStatus() {
        try {
            const response = await apiFetch("/api/status", undefined, { silent: true });
            if (!response.ok) return;
            const data = (await response.json()) as StatusResponse;
            profiles = data.profiles;
        } catch (error) {
            console.error("Failed to load profile status", error);
        }
    }

    async function loadHistory(mode: "replace" | "older" | "newer" = "replace") {
        if (!profile) return;
        const controller = new AbortController();
        if (mode === "replace") {
            currentAbort?.abort();
            currentAbort = controller;
            loading = groups.length === 0;
            refreshing = groups.length > 0;
        } else if (mode === "older") {
            if (!nextBeforeId) return;
            loadingOlder = true;
        }

        const params = historyParams(mode !== "older");
        if (mode === "older" && nextBeforeId)
            params.set("before_id", String(nextBeforeId));
        if (mode === "newer" && latestGroupId)
            params.set("after_id", String(latestGroupId));

        try {
            const response = await apiFetch(`${historyPath()}?${params.toString()}`, {
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = (await response.json()) as GetHistoryResponse;

            if (mode === "older") {
                groups = mergeGroups(groups, data.groups ?? []);
            } else if (mode === "newer") {
                groups = mergeGroups(data.groups ?? [], groups);
            } else {
                groups = data.groups ?? [];
            }

            hasMore = data.has_more;
            nextBeforeId = data.next_before_id ?? null;
            latestGroupId = data.latest_group_id ?? latestGroupId;
            if (data.stats) stats = data.stats;
            lastRefreshed = Date.now();
        } catch (error) {
            if (isAbortError(error)) return;
            console.error("Failed to load history", error);
            toast("Failed to load history", "error");
        } finally {
            if (currentAbort === controller) currentAbort = null;
            loading = false;
            refreshing = false;
            loadingOlder = false;
        }
    }

    async function setOutcomeFilter(key: string | null) {
        activeOutcome = key && activeOutcome !== key ? key : null;
        await resetHistory();
    }

    async function resetHistory() {
        groups = [];
        nextBeforeId = null;
        latestGroupId = null;
        await loadHistory("replace");
        openHistoryWs();
    }

    async function syncProfile(trigger: "manual" | "poll") {
        const response = await apiFetch(
            `/api/sync/profile/${encodeURIComponent(profile)}?trigger=${trigger}`,
            { method: "POST" },
            {
                successMessage:
                    trigger === "poll"
                        ? `Triggered poll sync for profile ${profile}`
                        : `Triggered full sync for profile ${profile}`,
            },
        );
        if (response.ok) await loadStatus();
    }

    async function reinitializeProfile() {
        if (
            !confirm(
                `Reinitialize profile ${profile}?\n\nThis will recreate its providers and restart its scheduler.`,
            )
        ) {
            return;
        }
        acting = true;
        try {
            const response = await apiFetch(
                `/api/sync/profile/${encodeURIComponent(profile)}/reinitialize`,
                { method: "POST" },
                { successMessage: `Reinitialized profile ${profile}` },
            );
            if (response.ok) await loadStatus();
        } finally {
            acting = false;
        }
    }

    async function retryGroup(group: HistoryGroup) {
        acting = true;
        try {
            const response = await apiFetch(
                `${historyPath()}/groups/${group.id}/retry`,
                { method: "POST" },
                { successMessage: "Retry queued" },
            );
            if (response.ok) await loadStatus();
        } finally {
            acting = false;
        }
    }

    async function deleteGroup(group: HistoryGroup) {
        if (!confirm(`Delete history group #${group.id}?`)) return;
        acting = true;
        try {
            const response = await apiFetch(
                `${historyPath()}/groups/${group.id}`,
                { method: "DELETE" },
                { successMessage: "Deleted history group" },
            );
            if (response.ok) groups = groups.filter((item) => item.id !== group.id);
        } finally {
            acting = false;
        }
    }

    async function undoOperation(operation: HistoryOperation) {
        if (!confirm(`Undo operation #${operation.id}?`)) return;
        acting = true;
        try {
            const response = await apiFetch(
                `${historyPath()}/operations/${operation.id}/undo`,
                { method: "POST" },
                { successMessage: "Undo queued" },
            );
            if (response.ok) await loadStatus();
        } finally {
            acting = false;
        }
    }

    async function deleteOperation(operation: HistoryOperation) {
        if (!confirm(`Delete operation #${operation.id}?`)) return;
        acting = true;
        try {
            const response = await apiFetch(
                `${historyPath()}/operations/${operation.id}`,
                { method: "DELETE" },
                { successMessage: "Deleted history operation" },
            );
            if (response.ok) await loadHistory("replace");
        } finally {
            acting = false;
        }
    }

    async function openPinManager() {
        if (!PinManagerComponent) {
            PinManagerComponent = (
                await import("$lib/components/pins/pin-manager.svelte")
            ).default;
        }
        pinManagerOpen = true;
    }

    function isOperation(
        target: HistoryGroup | HistoryOperation,
    ): target is HistoryOperation {
        return "resource_kind" in target;
    }

    function targetKey(target: HistoryGroup | HistoryOperation): string | null {
        if (isOperation(target)) return target.target_ref?.key ?? null;
        return target.target_parent_ref?.key ?? null;
    }

    function targetPinned(target: HistoryGroup | HistoryOperation): boolean {
        if (isOperation(target)) return !!target.pinned;
        return !!target.operations?.some((operation) => operation.pinned);
    }

    async function togglePin(target: HistoryGroup | HistoryOperation) {
        const key = targetKey(target);
        if (!key) return;
        const pinned = targetPinned(target);
        acting = true;
        try {
            const response = await apiFetch(
                pinPath(key),
                { method: pinned ? "DELETE" : "PUT" },
                { successMessage: pinned ? "Unpinned target" : "Pinned target" },
            );
            if (response.ok) await loadHistory("replace");
        } finally {
            acting = false;
        }
    }

    function openStatusWs() {
        if (destroyed) return;
        statusWs?.close();
        if (statusReconnect) clearTimeout(statusReconnect);
        statusWs = new WebSocket(buildWebSocketUrl("/ws/status"));
        statusWs.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data) as Partial<StatusResponse>;
                if (data.profiles) profiles = data.profiles;
            } catch {}
        };
        statusWs.onclose = () => {
            if (!destroyed) statusReconnect = setTimeout(openStatusWs, 2500);
        };
    }

    function openHistoryWs() {
        if (destroyed || !profile) return;
        historyWs?.close();
        if (historyReconnect) clearTimeout(historyReconnect);
        const params = new SvelteURLSearchParams();
        if (activeOutcome) params.set("outcome", activeOutcome);
        const suffix = params.toString() ? `?${params.toString()}` : "";
        historyWs = new WebSocket(
            buildWebSocketUrl(`/ws/history/${encodeURIComponent(profile)}${suffix}`),
        );
        historyWs.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data) as {
                    latest_group_id?: number | null;
                };
                const nextLatest = data.latest_group_id ?? null;
                if (nextLatest && latestGroupId && nextLatest > latestGroupId) {
                    void loadHistory("newer");
                } else if (nextLatest && !latestGroupId) {
                    void loadHistory("replace");
                }
            } catch {}
        };
        historyWs.onclose = () => {
            if (!destroyed) historyReconnect = setTimeout(openHistoryWs, 2500);
        };
    }

    onMount(() => {
        mounted = true;
        activeProfile = profile;
        void loadStatus();
        void loadHistory("replace");
        openStatusWs();
        openHistoryWs();

        return () => {
            destroyed = true;
            currentAbort?.abort();
            statusWs?.close();
            historyWs?.close();
            if (statusReconnect) clearTimeout(statusReconnect);
            if (historyReconnect) clearTimeout(historyReconnect);
        };
    });

    $effect(() => {
        if (!mounted || profile === activeProfile) return;
        activeProfile = profile;
        activeOutcome = null;
        pinManagerOpen = false;
        groups = [];
        nextBeforeId = null;
        latestGroupId = null;
        void loadStatus();
        void loadHistory("replace");
        openHistoryWs();
    });
</script>

<div class="space-y-6">
    <div class="space-y-2">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div class="space-y-1 sm:flex-1">
                <div class="flex flex-wrap items-center gap-2">
                    <History class="inline h-4 w-4 text-slate-300" />
                    <h2 class="text-lg font-semibold">Sync History</h2>
                    <span class="text-xs text-slate-500">
                        {profile}
                    </span>
                </div>
                <p class="text-xs text-slate-400">
                    Review sync history, inspect changes, and replay failed work.
                </p>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-[11px] sm:justify-end">
                <button
                    onclick={reinitializeProfile}
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-amber-600/60 bg-amber-600/30 px-2 py-1 font-medium text-amber-200 shadow-sm transition-colors hover:bg-amber-600/40 focus:ring-2 focus:ring-amber-500/40 focus:outline-none disabled:cursor-wait disabled:opacity-70 sm:px-3 sm:py-1.5"
                    disabled={acting}
                    ><Wrench
                        class={`inline h-4 w-4 text-[14px] ${acting ? "animate-spin" : ""}`} />
                    {acting ? "Reinitializing..." : "Reinitialize"}</button>
                <button
                    onclick={() => syncProfile("manual")}
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-emerald-600/60 bg-emerald-600/30 px-2 py-1 font-medium text-emerald-200 shadow-sm transition-colors hover:bg-emerald-600/40 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 sm:px-3 sm:py-1.5"
                    disabled={isProfileRunning || acting}
                    ><RefreshCcw class="inline h-4 w-4 text-[14px]" /> Full Scan</button>
                <button
                    onclick={() => syncProfile("poll")}
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-sky-600/60 bg-sky-600/30 px-2 py-1 font-medium text-sky-200 shadow-sm transition-colors hover:bg-sky-600/40 focus:ring-2 focus:ring-sky-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 sm:px-3 sm:py-1.5"
                    disabled={isProfileRunning || acting}
                    ><CloudDownload class="inline h-4 w-4 text-[14px]" /> Poll Scan</button>
                <button
                    onclick={() => void openPinManager()}
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700/70 bg-slate-900/60 px-2 py-1 font-medium text-slate-300 shadow-sm transition-colors hover:bg-slate-800 hover:text-slate-100 focus:ring-2 focus:ring-slate-500/40 focus:outline-none sm:px-3 sm:py-1.5"
                    ><Pin class="inline h-4 w-4 text-[14px]" /> Pins</button>
                <button
                    onclick={() => loadHistory("replace")}
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-600/60 bg-slate-700/40 px-2 py-1 font-medium text-slate-200 shadow-sm transition-colors hover:bg-slate-600/50 focus:ring-2 focus:ring-slate-500/40 focus:outline-none sm:px-3 sm:py-1.5"
                    ><RotateCw class="inline h-4 w-4 text-[14px]" /> Refresh</button>
            </div>
        </div>
        {#if hasRunningSync()}
            <div class="mt-2 space-y-2">
                <div
                    class="flex items-center justify-between text-[11px] text-slate-400">
                    <div class="truncate">
                        {#if progressSubject(currentSync)}
                            <span class="text-slate-300"
                                >{progressSubject(currentSync)}</span>
                            <span class="mx-1">•</span>
                        {/if}
                        <span class="tracking-wide uppercase"
                            >{progressStage(currentSync)}</span>
                    </div>
                    <div>
                        {progressCount(currentSync)}
                    </div>
                </div>
                {#key currentSync?.started_at}
                    {#if isDeterminate()}
                        <Meter.Root
                            value={percent()}
                            min={0}
                            max={1}
                            class="h-2 w-full overflow-hidden rounded bg-slate-800/80">
                            <div
                                class="h-full bg-linear-to-r from-indigo-500 via-sky-500 to-cyan-400 transition-all duration-300 ease-out"
                                style="transform: translateX(-{100 -
                                    100 * percent()}%)">
                            </div>
                        </Meter.Root>
                    {:else}
                        <div class="h-2 w-full overflow-hidden rounded bg-slate-800/80">
                            <div
                                class="sync-progress-indeterminate h-full w-1/3 bg-linear-to-r from-indigo-500 via-sky-500 to-cyan-400">
                            </div>
                        </div>
                    {/if}
                {/key}
            </div>
        {/if}
    </div>

    <section class="space-y-3">
        <TimelineOutcomeFilters
            meta={outcomeFilterMeta}
            {stats}
            active={activeOutcome}
            onToggle={(key: string) => void setOutcomeFilter(key)}
            onClear={() => void setOutcomeFilter(null)} />
        <div class="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            <span>updated {formatTimeAgo(lastRefreshed)}</span>
            {#if refreshing}
                <span class="inline-flex items-center gap-1 text-sky-300">
                    <LoaderCircle class="h-3 w-3 animate-spin" /> refreshing
                </span>
            {/if}
        </div>
    </section>

    <section class="space-y-4">
        {#if loading && groups.length === 0}
            {#each [1, 2, 3] as item (item)}
                <div
                    in:fade={{ duration: 150 }}
                    class="animate-pulse rounded-md border border-slate-800/60 bg-slate-900/40 p-4">
                    <div class="flex items-center gap-2">
                        <div class="h-5 w-20 rounded bg-slate-800/80"></div>
                        <div class="h-3 w-36 rounded bg-slate-800/60"></div>
                    </div>
                    <div class="mt-4 grid gap-3 lg:grid-cols-[1fr_auto_1fr]">
                        <div class="flex h-32 gap-3 rounded-md bg-slate-800/40 p-2.5">
                            <div class="h-full w-19 rounded-md bg-slate-700/50"></div>
                            <div class="flex-1 space-y-2 py-2">
                                <div class="h-3 w-20 rounded bg-slate-700/50"></div>
                                <div class="h-4 w-2/3 rounded bg-slate-700/50"></div>
                                <div class="h-3 w-1/2 rounded bg-slate-700/40"></div>
                            </div>
                        </div>
                        <div class="hidden h-24 w-4 rounded bg-slate-900 lg:block">
                        </div>
                        <div class="flex h-32 gap-3 rounded-md bg-slate-800/40 p-2.5">
                            <div class="h-full w-19 rounded-md bg-slate-700/50"></div>
                            <div class="flex-1 space-y-2 py-2">
                                <div class="h-3 w-20 rounded bg-slate-700/50"></div>
                                <div class="h-4 w-2/3 rounded bg-slate-700/50"></div>
                                <div class="h-3 w-1/2 rounded bg-slate-700/40"></div>
                            </div>
                        </div>
                    </div>
                </div>
            {/each}
        {:else if groups.length === 0}
            <div
                in:fade={{ duration: 150 }}
                class="flex flex-col items-center justify-center rounded-md border-2 border-dashed border-slate-700/70 bg-slate-900/30 p-8 text-center">
                <CircleSlash class="h-8 w-8 text-slate-500" />
                <div class="mt-3 text-sm font-medium text-slate-300">
                    No history entries
                </div>
                <p class="mt-1 max-w-md text-xs text-slate-500">
                    This profile has no history matching the active filters yet.
                </p>
            </div>
        {:else}
            {#each groups as group (group.id)}
                <TimelineGroupCard
                    {group}
                    disabled={acting}
                    onRetry={retryGroup}
                    onDeleteGroup={deleteGroup}
                    onUndoOperation={undoOperation}
                    onDeleteOperation={deleteOperation}
                    onTogglePin={togglePin} />
            {/each}
        {/if}

        {#if hasMore}
            <div class="flex justify-center">
                <button
                    type="button"
                    class="inline-flex items-center gap-2 rounded-md border border-slate-700/70 bg-slate-800/60 px-4 py-2 text-sm font-medium text-slate-200 transition-colors hover:bg-slate-700 disabled:cursor-wait disabled:opacity-60"
                    onclick={() => loadHistory("older")}
                    disabled={loadingOlder}>
                    {#if loadingOlder}<LoaderCircle class="h-4 w-4 animate-spin" />{/if}
                    Load older
                </button>
            </div>
        {/if}
    </section>
</div>

{#if pinManagerOpen && PinManagerComponent}
    {@const PinManager = PinManagerComponent}
    <PinManager
        {profile}
        onClose={() => (pinManagerOpen = false)}
        onChanged={() => void loadHistory("replace")} />
{/if}

<style>
    .sync-progress-indeterminate {
        animation: sync-progress-indeterminate 1.2s ease-in-out infinite;
    }

    @keyframes sync-progress-indeterminate {
        0% {
            transform: translateX(-120%);
        }
        100% {
            transform: translateX(320%);
        }
    }
</style>
