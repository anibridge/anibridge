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
    import type { CurrentSync, HistoryItem } from "$lib/types/api";
    import { apiFetch } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    const { params } = $props<{ params: { profile: string } }>();

    let items: HistoryItem[] = $state([]);
    let stats: Record<string, number> = $state({});
    let loadingInitial = $state(true);
    let loadingMore = $state(false);
    let page = $state(1);
    let pages = $state(1);
    let perPage = $state(50);
    let outcomeFilter: string | null = $state("synced");
    let showJump = $state(false);
    let newItemsCount = $state(0);
    let ws: WebSocket | null = null;
    let statusWs: WebSocket | null = null;
    let knownIds = new SvelteSet<number>();
    let sentinel: HTMLDivElement | null = $state(null);
    let openDiff: Record<number, boolean> = $state({});
    let currentSync: CurrentSync | null = $state(null);
    let isProfileRunning = $state(false);
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

    const buildQuery = (p: number) => {
        const u = new SvelteURLSearchParams({
            page: String(p),
            per_page: String(perPage),
        });
        if (outcomeFilter) u.set("outcome", outcomeFilter);
        return `/api/history/${params.profile}?${u}`;
    };

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
        if (!item) return false;
        if (item.ephemeral) return false;
        if (!item.list_media_key || !item.list_namespace) return false;
        return item.outcome === "synced" || item.outcome === "deleted";
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
        if (!item) return false;
        return item.outcome === "failed" || item.outcome === "not_found";
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
        if (!item) return false;
        if (!(item.before_state || item.after_state)) return false;
        return item.outcome === "synced" || item.outcome === "undone";
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

    async function loadFirst() {
        loadingInitial = true;
        try {
            const r = await apiFetch(buildQuery(1));
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = await r.json();
            items = d.items || [];
            stats = d.stats || {};
            page = d.page || 1;
            pages = d.pages || 1;
            perPage = d.per_page || perPage;
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
        if (loadingMore || page >= pages) return;
        loadingMore = true;
        const next = page + 1;
        try {
            const r = await apiFetch(buildQuery(next));
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = await r.json();
            const existing = new SvelteSet(items.map((i: HistoryItem) => i.id));
            const newOnes = (d.items || []).filter(
                (i: HistoryItem) => !existing.has(i.id),
            );
            items = [...items, ...newOnes];
            stats = d.stats || stats;
            page = d.page || next;
            pages = d.pages || pages;
            perPage = d.per_page || perPage;
            newOnes.forEach((i: HistoryItem) => knownIds.add(i.id));
        } catch (e) {
            console.error(e);
        } finally {
            loadingMore = false;
        }
    }

    function toggleOutcomeFilter(k: string) {
        outcomeFilter = outcomeFilter === k ? null : k;
        loadFirst();
    }

    function initWs() {
        try {
            ws?.close();
        } catch {}
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${proto}//${location.host}/ws/history/${params.profile}`);
        ws.onmessage = (ev) => {
            try {
                const d = JSON.parse(ev.data);
                if (!Array.isArray(d.items)) return;
                let added = 0;
                for (const it of d.items) {
                    if (
                        !knownIds.has(it.id) &&
                        (!outcomeFilter || it.outcome === outcomeFilter)
                    ) {
                        // Apply outcomeFilter to WebSocket data
                        items = [it, ...items];
                        knownIds.add(it.id);
                        added++;
                    }
                }
                if (added && !isNearTop) newItemsCount += added;
                handleScroll();
            } catch {}
        };
    }

    function initStatusWs() {
        try {
            statusWs?.close();
        } catch {}
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        statusWs = new WebSocket(`${proto}//${location.host}/ws/status`);
        statusWs.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data);
                const prof = data?.profiles?.[params.profile];
                const cs = prof?.status?.current_sync;
                currentSync = cs ?? null;
                isProfileRunning = !!(
                    prof && prof.status?.current_sync?.state === "running"
                );
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

    onMount(() => {
        loadFirst();
        initWs();
        initStatusWs();
        const io = new IntersectionObserver((entries) => {
            for (const e of entries) if (e.isIntersecting) loadMore();
        });
        if (sentinel) io.observe(sentinel);
        addEventListener("scroll", handleScroll, { passive: true });
        return () => {
            try {
                ws?.close();
                statusWs?.close();
            } catch {}
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
        onFullSync={() => triggerSync(false)}
        onPollSync={() => triggerSync(true)}
        onRefresh={loadFirst} />
    <div class="-mt-1">
        <TimelineGlobalPinsManager profile={params.profile} />
    </div>
    <TimelineOutcomeFilters
        meta={OUTCOME_META}
        {stats}
        active={outcomeFilter}
        onToggle={toggleOutcomeFilter}
        onClear={() => ((outcomeFilter = null), loadFirst())} />

    <div
        class="flex items-center gap-2 text-[11px] text-slate-500"
        hidden={!items.length}>
        <span class="inline-flex items-center gap-1"
            ><InfinityIcon class="inline h-4 w-4" /> Scroll to load older history</span>
        {#if loadingMore}
            <span class="inline-flex items-center gap-1 text-sky-300"
                ><LoaderCircle class="inline h-4 w-4 animate-spin" /> Loading…</span>
        {/if}
        {#if !loadingMore && page >= pages}
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
