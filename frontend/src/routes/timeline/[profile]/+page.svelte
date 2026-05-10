<script lang="ts">
    import { onMount } from "svelte";

    import {
        ArrowUp,
        Check,
        Circle,
        CircleCheck,
        CircleX,
        Infinity as InfinityIcon,
        LoaderCircle,
        RotateCw,
        SearchX,
        Trash2,
    } from "@lucide/svelte";
    import { SvelteSet, SvelteURLSearchParams } from "svelte/reactivity";

    import TimelineGlobalPinsManager from "$lib/components/timeline/timeline-global-pins-manager.svelte";
    import TimelineHeader from "$lib/components/timeline/timeline-header.svelte";
    import TimelineItem from "$lib/components/timeline/timeline-item.svelte";
    import TimelineOutcomeFilters from "$lib/components/timeline/timeline-outcome-filters.svelte";
    import type { ItemDiffUi } from "$lib/components/timeline/types";
    import type {
        CurrentSync,
        GetHistoryResponse,
        HistoryItem,
        StatusResponse,
    } from "$lib/types/api";
    import { apiFetch, apiJson, buildWebSocketUrl } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    const { params } = $props<{ params: { profile: string } }>();

    let items: HistoryItem[] = $state([]);
    let stats: Record<string, number> = $state({});
    let loadingInitial = $state(true);
    let loadingMore = $state(false);
    let loadingNew = $state(false);
    let limit = $state(25);
    let hasMore = $state(false);
    let nextBeforeId: number | null = $state(null);
    let latestId: number | null = $state(null);
    let outcomeFilter: string | null = $state("synced");
    let showJump = $state(false);
    let newItemsCount = $state(0);
    let ws: WebSocket | null = null;
    let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let wsShouldReconnect = true;
    let statusWs: WebSocket | null = null;
    let knownIds = new SvelteSet<number>();
    let sentinel: HTMLDivElement | null = $state(null);
    let openDiff: Record<number, boolean> = $state({});
    let currentSync: CurrentSync | null = $state(null);
    let isProfileRunning = $state(false);
    let isReinitializing = $state(false);
    let undoLoading: Record<number, boolean> = $state({});
    let retryLoading: Record<number, boolean> = $state({});

    let openPins: Record<number, boolean> = $state({});
    let pinDraftCounts: Record<number, number> = $state({});
    let pinBusy: Record<number, boolean> = $state({});

    let diffUi: Record<number, ItemDiffUi> = $state({});

    function ensureDiffUi(id: number): ItemDiffUi {
        return (diffUi[id] ??= { tab: "changes", filter: "", showUnchanged: false });
    }

    function toggleDiff(id: number) {
        openDiff[id] = !openDiff[id];
        ensureDiffUi(id);
    }

    interface OutcomeMeta {
        label: string;
        color: string;
        icon: typeof Circle;
        order: number;
    }
    const OUTCOME_META: Record<string, OutcomeMeta> = {
        synced: {
            label: "Synced",
            color: "bg-emerald-600/80",
            icon: CircleCheck,
            order: 0,
        },
        failed: { label: "Failed", color: "bg-red-600/80", icon: CircleX, order: 1 },
        not_found: {
            label: "Not Found",
            color: "bg-amber-500/80",
            icon: SearchX,
            order: 2,
        },
        deleted: { label: "Deleted", color: "bg-rose-600/80", icon: Trash2, order: 3 },
        undone: {
            label: "Undone",
            color: "bg-violet-600/80",
            icon: RotateCw,
            order: 6,
        },
    };

    function metaFor(o: string) {
        return (
            OUTCOME_META[o] ?? {
                label: o,
                color: "bg-slate-600/70",
                icon: Circle,
                order: 999,
            }
        );
    }

    const buildQuery = (opts?: {
        beforeId?: number | null;
        afterId?: number | null;
        includeStats?: boolean;
        limitOverride?: number;
    }) => {
        const u = new SvelteURLSearchParams({
            limit: String(opts?.limitOverride ?? limit),
        });
        if (typeof opts?.beforeId === "number") {
            u.set("before_id", String(opts.beforeId));
        }
        if (typeof opts?.afterId === "number") {
            u.set("after_id", String(opts.afterId));
        }
        if (outcomeFilter) u.set("outcome", outcomeFilter);
        if (opts?.includeStats) u.set("include_stats", "true");
        return `/api/history/${params.profile}?${u}`;
    };

    const resetWsReconnectTimer = () => {
        if (!wsReconnectTimer) return;
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = null;
    };

    function mergeNewest(itemsToPrepend: HistoryItem[]): number {
        let added = 0;
        const deduped: HistoryItem[] = [];
        for (const item of itemsToPrepend) {
            if (knownIds.has(item.id)) continue;
            knownIds.add(item.id);
            deduped.push(item);
            added++;
        }
        if (deduped.length) items = [...deduped, ...items];
        return added;
    }

    function displayTitle(item: HistoryItem) {
        return (
            item.list_media?.title ??
            item.library_media?.title ??
            (item.list_namespace && item.list_media_key
                ? `${item.list_namespace}:${item.list_media_key}`
                : null) ??
            (item.library_namespace && item.library_media_key
                ? `${item.library_namespace}:${item.library_media_key}`
                : null) ??
            "Unknown title"
        );
    }

    function coverImage(item: HistoryItem) {
        return item.list_media?.poster_url ?? item.library_media?.poster_url ?? null;
    }

    async function deleteHistory(item: HistoryItem) {
        if (!confirm("Delete this history entry?")) return;
        try {
            const res = await apiFetch(`/api/history/${params.profile}/${item.id}`, {
                method: "DELETE",
            });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            // Remove locally
            items = items.filter((i) => i.id !== item.id);
            knownIds.delete(item.id);
            // Adjust stats
            const oc = data.outcome || item.outcome;
            if (oc) stats[oc] = Math.max(0, (stats[oc] || 1) - 1);
            toast("History entry deleted", "success");
        } catch (e) {
            toast("Delete failed", "error");
            console.error(e);
        }
    }

    function canUndo(item: HistoryItem): boolean {
        return !!(
            item &&
            !item.ephemeral &&
            item.list_media_key &&
            item.list_namespace &&
            (item.outcome === "synced" || item.outcome === "deleted")
        );
    }

    async function undoHistory(item: HistoryItem) {
        if (!canUndo(item) || undoLoading[item.id]) return;
        undoLoading[item.id] = true;
        try {
            const res = await apiFetch(
                `/api/history/${params.profile}/${item.id}/undo`,
                { method: "POST" },
            );
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = (await res.json()) as { item?: HistoryItem };
            if (data?.item) {
                items = [data.item, ...items];
                knownIds.add(data.item.id);
                stats[data.item.outcome] = (stats[data.item.outcome] || 0) + 1;
            }
            toast("Undo applied", "success");
        } catch (e) {
            toast("Undo failed", "error");
            console.error(e);
        } finally {
            undoLoading[item.id] = false;
        }
    }

    function canRetry(item: HistoryItem): boolean {
        return !!(item && (item.outcome === "failed" || item.outcome === "not_found"));
    }

    async function retryHistory(item: HistoryItem) {
        if (!canRetry(item) || retryLoading[item.id]) return;
        retryLoading[item.id] = true;
        try {
            const res = await apiFetch(
                `/api/history/${params.profile}/${item.id}/retry`,
                { method: "POST" },
            );
            if (!res.ok) throw new Error("HTTP " + res.status);
            toast("Retry queued", "success");
        } catch (e) {
            toast("Retry failed", "error");
            console.error(e);
        } finally {
            retryLoading[item.id] = false;
        }
    }

    function canShowDiff(item: HistoryItem): boolean {
        // Diff panel should be available for original sync changes and subsequent undo entries
        return !!(
            item &&
            (item.before_state || item.after_state) &&
            (item.outcome === "synced" || item.outcome === "undone")
        );
    }

    function diffCountFor(item: HistoryItem): number {
        let count = 0;
        const before = item.before_state || {};
        const after = item.after_state || {};
        const keys = new Set<string>([...Object.keys(before), ...Object.keys(after)]);
        for (const k of keys) {
            if (JSON.stringify(before[k]) !== JSON.stringify(after[k])) {
                count++;
            }
        }
        return count;
    }

    function getListIdentifier(
        item: HistoryItem,
    ): { namespace: string; mediaKey: string } | null {
        const namespace = item.list_namespace ?? item.list_media?.namespace ?? null;
        const mediaKey = item.list_media_key ?? item.list_media?.key ?? null;
        if (!namespace || !mediaKey) return null;
        return { namespace, mediaKey };
    }

    function applyPins(namespace: string, mediaKey: string, fields: string[]) {
        items = items.map((entry) =>
            entry.list_namespace === namespace && entry.list_media_key === mediaKey
                ? { ...entry, pinned_fields: fields.length ? [...fields] : null }
                : entry,
        );
    }

    function pinCountFor(item: HistoryItem): number {
        const draft = pinDraftCounts[item.id];
        if (typeof draft === "number") return draft;
        return Array.isArray(item.pinned_fields) ? item.pinned_fields.length : 0;
    }

    function handlePinsDraft(item: HistoryItem, fields: string[]) {
        pinDraftCounts[item.id] = fields.length;
    }

    function handlePinsSaved(item: HistoryItem, fields: string[]) {
        const identifier = getListIdentifier(item);
        if (identifier) applyPins(identifier.namespace, identifier.mediaKey, fields);
        pinDraftCounts[item.id] = fields.length;
    }

    function handlePinsBusy(item: HistoryItem, value: boolean) {
        pinBusy[item.id] = value;
    }

    function togglePinsPanel(item: HistoryItem) {
        const identifier = getListIdentifier(item);
        if (!identifier) {
            toast("Pins require a linked list entry", "warn");
            return;
        }
        const next = !openPins[item.id];
        openPins[item.id] = next;
        if (next) {
            pinDraftCounts[item.id] = Array.isArray(item.pinned_fields)
                ? item.pinned_fields.length
                : 0;
        } else {
            delete pinDraftCounts[item.id];
            delete pinBusy[item.id];
        }
    }

    let isNearTop = $state(true);

    function handleScroll() {
        isNearTop = window.scrollY < 120;
        if (isNearTop) newItemsCount = 0;
        showJump = !isNearTop && (newItemsCount > 0 || window.scrollY > 400);
    }

    async function refreshProfileStatus() {
        try {
            const data = await apiJson<StatusResponse>("/api/status");
            const profile = data.profiles?.[params.profile];
            const current = profile?.status?.current_sync ?? null;
            currentSync = current;
            isProfileRunning = current?.state === "running";
        } catch (e) {
            console.error("Failed to refresh profile status", e);
        }
    }

    async function loadFirst() {
        loadingInitial = true;
        try {
            const r = await apiFetch(buildQuery({ includeStats: true }));
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = (await r.json()) as GetHistoryResponse;
            items = d.items || [];
            stats = d.stats || {};
            limit = d.limit || 25;
            hasMore = !!d.has_more;
            latestId = d.latest_id ?? items[0]?.id ?? null;
            nextBeforeId =
                d.next_before_id ??
                (hasMore && items.length ? items[items.length - 1].id : null);
            knownIds = new SvelteSet(items.map((i) => i.id));
            openPins = {};
            pinDraftCounts = {};
            pinBusy = {};
            newItemsCount = 0;
        } catch (e) {
            console.error(e);
        } finally {
            loadingInitial = false;
        }
    }

    async function loadMore() {
        if (loadingMore || !hasMore || nextBeforeId === null) return;
        loadingMore = true;
        try {
            const r = await apiFetch(
                buildQuery({ beforeId: nextBeforeId, includeStats: false }),
            );
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = (await r.json()) as GetHistoryResponse;
            const newOnes = (d.items || []).filter(
                (i: HistoryItem) => !knownIds.has(i.id),
            );
            items = [...items, ...newOnes];
            hasMore = !!d.has_more;
            nextBeforeId = d.next_before_id ?? null;
            newOnes.forEach((i: HistoryItem) => knownIds.add(i.id));
        } catch (e) {
            console.error(e);
        } finally {
            loadingMore = false;
        }
    }

    async function loadNewer() {
        if (loadingNew) return;
        const currentTopId = items[0]?.id;
        if (!currentTopId) return;

        loadingNew = true;
        try {
            const r = await apiFetch(
                buildQuery({
                    afterId: currentTopId,
                    includeStats: true,
                    limitOverride: 250,
                }),
            );
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = (await r.json()) as GetHistoryResponse;
            const added = mergeNewest(d.items || []);
            if (added && !isNearTop) newItemsCount += added;
            if (d.stats) stats = d.stats;
            if (typeof d.latest_id === "number") latestId = d.latest_id;
            handleScroll();
        } catch (e) {
            console.error(e);
        } finally {
            loadingNew = false;
        }
    }

    function toggleOutcomeFilter(k: string) {
        outcomeFilter = outcomeFilter === k ? null : k;
        loadFirst();
        initWs();
    }

    function initWs() {
        try {
            ws?.close();
        } catch {}
        resetWsReconnectTimer();

        const query = new SvelteURLSearchParams();
        if (outcomeFilter) query.set("outcome", outcomeFilter);
        const querySuffix = query.toString() ? `?${query}` : "";
        ws = new WebSocket(
            buildWebSocketUrl(`/ws/history/${params.profile}${querySuffix}`),
        );
        ws.onmessage = (ev) => {
            try {
                const d = JSON.parse(ev.data);
                if (typeof d.latest_id !== "number") return;
                if (latestId === null) {
                    latestId = d.latest_id;
                    void loadFirst();
                    return;
                }
                if (d.latest_id <= latestId) return;
                latestId = d.latest_id;
                void loadNewer();
            } catch {}
        };
        ws.onclose = () => {
            if (!wsShouldReconnect) return;
            wsReconnectTimer = setTimeout(initWs, 2000);
        };
    }

    function initStatusWs() {
        try {
            statusWs?.close();
        } catch {}
        statusWs = new WebSocket(buildWebSocketUrl("/ws/status"));
        statusWs.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data);
                const prof = data?.profiles?.[params.profile];
                const cs = prof?.status?.current_sync;
                currentSync = cs ?? null;
                isProfileRunning = prof?.status?.current_sync?.state === "running";
            } catch {}
        };
        statusWs.onclose = () => {
            setTimeout(initStatusWs, 2000);
        };
    }

    function jumpToLatest() {
        window.scrollTo({ top: 0, behavior: "smooth" });
        setTimeout(() => {
            newItemsCount = 0;
            handleScroll();
        }, 400);
    }

    async function triggerSync(poll: boolean) {
        try {
            await apiFetch(
                `/api/sync/profile/${params.profile}?poll=${poll}`,
                { method: "POST" },
                {
                    successMessage: poll
                        ? `Triggered poll sync for profile ${params.profile}`
                        : `Triggered full sync for profile ${params.profile}`,
                },
            );
        } catch {
            toast("Sync failed", "error");
        }
    }

    async function reinitializeProfile() {
        if (isReinitializing) return;
        if (
            !confirm(
                `Reinitialize profile ${params.profile}?\n\nThis will recreate its providers and restart its scheduler.`,
            )
        ) {
            return;
        }

        isReinitializing = true;
        try {
            const response = await apiFetch(
                `/api/sync/profile/${params.profile}/reinitialize`,
                { method: "POST" },
                { successMessage: `Reinitialized profile ${params.profile}` },
            );
            if (!response.ok) return;
            await refreshProfileStatus();
        } catch (e) {
            console.error("Failed to reinitialize profile", e);
        } finally {
            isReinitializing = false;
        }
    }

    onMount(() => {
        wsShouldReconnect = true;
        loadFirst();
        refreshProfileStatus();
        initWs();
        initStatusWs();
        const io = new IntersectionObserver((entries) => {
            for (const e of entries) if (e.isIntersecting) loadMore();
        });
        if (sentinel) io.observe(sentinel);
        addEventListener("scroll", handleScroll, { passive: true });
        return () => {
            wsShouldReconnect = false;
            try {
                ws?.close();
                statusWs?.close();
            } catch {}
            resetWsReconnectTimer();
            removeEventListener("scroll", handleScroll);
            io.disconnect();
        };
    });
</script>

<div class="space-y-6">
    <TimelineHeader
        profile={params.profile}
        {currentSync}
        {isProfileRunning}
        {isReinitializing}
        onFullSync={() => triggerSync(false)}
        onPollSync={() => triggerSync(true)}
        onReinitialize={reinitializeProfile}
        onRefresh={loadFirst} />
    <div class="-mt-1">
        <TimelineGlobalPinsManager profile={params.profile} />
    </div>
    <TimelineOutcomeFilters
        meta={OUTCOME_META}
        {stats}
        active={outcomeFilter}
        onToggle={toggleOutcomeFilter}
        onClear={() => ((outcomeFilter = null), loadFirst(), initWs())} />

    <div
        class="flex items-center gap-2 text-[11px] text-slate-500"
        hidden={!items.length}>
        <span class="inline-flex items-center gap-1"
            ><InfinityIcon class="inline h-4 w-4" /> Scroll to load older history</span>
        {#if loadingMore}
            <span class="inline-flex items-center gap-1 text-sky-300"
                ><LoaderCircle class="inline h-4 w-4 animate-spin" /> Loading…</span>
        {/if}
        {#if !loadingMore && !hasMore}
            <span class="inline-flex items-center gap-1 text-emerald-400"
                ><Check class="inline h-4 w-4" /> All loaded</span>
        {/if}
    </div>
    <div
        class="space-y-4"
        class:hidden={!items.length && !loadingInitial}>
        {#each items as item (item.id)}
            {@const meta = metaFor(item.outcome)}
            <TimelineItem
                profile={params.profile}
                {item}
                {meta}
                {isProfileRunning}
                {displayTitle}
                {coverImage}
                {canRetry}
                {retryHistory}
                retryLoading={retryLoading[item.id] || false}
                {canUndo}
                {undoHistory}
                undoLoading={undoLoading[item.id] || false}
                {deleteHistory}
                {canShowDiff}
                {toggleDiff}
                openDiff={openDiff[item.id] || false}
                {ensureDiffUi}
                diffCount={diffCountFor(item)}
                hasPins={Boolean(getListIdentifier(item))}
                togglePins={togglePinsPanel}
                openPins={openPins[item.id] || false}
                pinButtonLoading={pinBusy[item.id] || false}
                pinCount={pinCountFor(item)}
                onPinsDraft={handlePinsDraft}
                onPinsSaved={handlePinsSaved}
                onPinsBusy={handlePinsBusy} />
        {/each}
    </div>
    {#if !items.length && !loadingInitial}
        <p class="text-sm text-slate-500">No history yet.</p>
    {/if}
    <div bind:this={sentinel}></div>
    {#if showJump}
        <div class="fixed right-6 bottom-6 z-40">
            <button
                onclick={jumpToLatest}
                class="pointer-events-auto flex items-center gap-2 rounded-md border border-sky-500/60 bg-linear-to-r from-sky-600 to-sky-500 py-2 pr-3 pl-3 text-sm font-medium text-white shadow-md shadow-slate-950/40 backdrop-blur-md hover:from-sky-500 hover:to-sky-400">
                <ArrowUp class="inline h-4 w-4" />
                <span class="hidden sm:inline">Latest</span>
                {#if newItemsCount > 0}
                    <span
                        class="inline-flex h-5 min-w-5 items-center justify-center rounded-md border border-white/20 bg-slate-900/70 px-1 text-[10px] leading-none font-semibold text-white shadow ring-1 ring-sky-300/40"
                        >{newItemsCount}</span>
                {/if}
            </button>
        </div>
    {/if}
</div>
