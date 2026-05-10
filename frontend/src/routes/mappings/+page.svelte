<script lang="ts">
    import { onMount } from "svelte";

    import { Check, Eye, List, RefreshCcw } from "@lucide/svelte";
    import { Checkbox, Popover } from "bits-ui";
    import { SvelteSet, SvelteURLSearchParams } from "svelte/reactivity";

    import { browser } from "$app/environment";
    import { goto } from "$app/navigation";
    import { resolve } from "$app/paths";
    import { clearCapabilitiesCache } from "$lib/components/mappings/capabilities-cache";
    import {
        COLUMNS_STORAGE_KEY,
        STATIC_COLUMNS,
        type ColumnConfig,
    } from "$lib/components/mappings/columns";
    import EditModal from "$lib/components/mappings/edit-modal.svelte";
    import IncludesModal from "$lib/components/mappings/includes-modal.svelte";
    import MappingsTable from "$lib/components/mappings/mappings-table.svelte";
    import SearchBar from "$lib/components/mappings/tool-bar.svelte";
    import Pagination from "$lib/components/pagination.svelte";
    import type { Mapping } from "$lib/types/api";
    import { apiFetch, isAbortError } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    let items: Mapping[] = $state([]);
    let total = $state(0);
    let page = $state(1);
    let pages = $state(1);
    let perPage = $state(25);
    let query = $state("");
    let customOnly = $state(false);
    let loading = $state(false);

    let currentAbort: AbortController | null = null;

    let editorOpen = $state(false);
    let editorMode = $state<"create" | "edit">("create");
    let editorTarget = $state<Mapping | null>(null);
    let includesOpen = $state(false);

    let pendingReplaceState: boolean | null = null;
    let searchBarKey = $state(0);

    function queuePushState() {
        pendingReplaceState = false;
    }

    function syncQueryParam(replaceState: boolean) {
        if (!browser) return;
        const params = new SvelteURLSearchParams();
        const trimmed = query.trim();
        if (trimmed) params.set("q", query);
        const search = params.toString();
        let target = resolve("/mappings");
        if (search) target += `?${search}`;
        const current = window.location.pathname + window.location.search;
        if (current === target) return;
        goto(target, { replaceState, keepFocus: true, noScroll: true });
    }

    async function load(trigger?: Event | { type: string }) {
        if (
            trigger &&
            "preventDefault" in trigger &&
            typeof trigger.preventDefault === "function"
        ) {
            trigger.preventDefault();
        }
        const controller = new AbortController();
        if (currentAbort) {
            currentAbort.abort();
        }
        currentAbort = controller;
        loading = true;
        const replaceState = pendingReplaceState ?? true;
        pendingReplaceState = null;
        syncQueryParam(replaceState);
        try {
            const p = new SvelteURLSearchParams({
                page: String(page),
                per_page: String(perPage),
            });
            if (query) p.set("q", query);
            if (customOnly) p.set("custom_only", "true");
            p.set("with_anilist", "true");
            const r = await apiFetch("/api/mappings?" + p.toString(), {
                signal: controller.signal,
            });
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = await r.json();
            items = d.items || [];
            columns = buildColumnsFromItems(items, columns);
            total = d.total || 0;
            pages = d.pages || 1;
            page = d.page || page;
        } catch (e) {
            if (isAbortError(e)) return;
            console.error("load mappings failed", e);
            toast("Failed to load mappings", "error");
        } finally {
            if (currentAbort === controller) {
                currentAbort = null;
                loading = false;
            }
        }
    }

    async function navigateToQuery(nextQuery: string) {
        const value = nextQuery.trim();
        if (!value) return;
        query = value;
        page = 1;
        queuePushState();
        await load();
    }

    function handleSearchSubmit() {
        queuePushState();
    }

    function cancelLoad() {
        if (!currentAbort) return;
        currentAbort.abort();
        currentAbort = null;
        loading = false;
    }

    function openCreateEditor() {
        editorMode = "create";
        editorTarget = null;
        editorOpen = true;
    }

    function openIncludesEditor() {
        includesOpen = true;
    }

    function handleEdit({ mapping }: { mapping: Mapping }) {
        editorMode = "edit";
        editorTarget = mapping;
        editorOpen = true;
    }

    async function handleSaved() {
        editorOpen = false;
        clearCapabilitiesCache();
        searchBarKey += 1;
        await load();
    }

    async function handleIncludesSaved() {
        includesOpen = false;
        clearCapabilitiesCache();
        searchBarKey += 1;
        await load();
    }

    let columns = $state<ColumnConfig[]>(restoreColumns());

    function restoreColumns(): ColumnConfig[] {
        try {
            const raw = localStorage.getItem(COLUMNS_STORAGE_KEY);
            if (raw) {
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    return parsed as ColumnConfig[];
                }
            }
        } catch {}
        return [...STATIC_COLUMNS];
    }

    function collectProviders(list: Mapping[]): string[] {
        const providers = new SvelteSet<string>();
        for (const item of list) {
            if (item.provider) providers.add(item.provider);
            for (const edge of item.edges || []) {
                providers.add(edge.target_provider);
            }
        }
        return Array.from(providers).sort();
    }

    function normalizeColumnTitle(t: string): string {
        return t.toUpperCase().replaceAll("_", " ").trim();
    }

    function buildColumnsFromItems(
        list: Mapping[],
        existing: ColumnConfig[],
    ): ColumnConfig[] {
        const stored = new Map(existing.map((c) => [c.id, c]));
        const providers = collectProviders(list);

        const dynamic = providers.map(
            (p) =>
                ({
                    id: `provider:${p}`,
                    title: normalizeColumnTitle(p),
                    visible: true,
                    width: 200,
                    minWidth: 100,
                    resizable: true,
                }) satisfies ColumnConfig,
        );

        const ordered = [
            STATIC_COLUMNS[0],
            ...dynamic,
            STATIC_COLUMNS[1],
            STATIC_COLUMNS[2],
        ];

        return ordered.map((col) => ({ ...col, ...(stored.get(col.id) || {}) }));
    }

    function hideAllColumns() {
        columns = columns.map((c) => ({ ...c, visible: false }));
    }

    function showAllColumns() {
        columns = columns.map((c) => ({ ...c, visible: true }));
    }

    function resetColumns() {
        columns = buildColumnsFromItems(items, STATIC_COLUMNS);
    }

    $effect(() => {
        try {
            localStorage.setItem(COLUMNS_STORAGE_KEY, JSON.stringify(columns));
        } catch {}
    });

    onMount(() => {
        if (!browser) return;
        try {
            const params = new SvelteURLSearchParams(window.location.search);
            const initial = params.get("q");
            if (initial !== null) {
                query = initial;
            }
        } catch (error) {
            console.error("Failed to parse initial mappings query", error);
        }
        load();
    });
</script>

<div class="space-y-6">
    <div class="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div class="space-y-1 sm:flex-1">
            <div class="flex items-center gap-2">
                <List class="inline h-4 w-4 text-slate-300" />
                <h2 class="text-lg font-semibold">Mappings</h2>
            </div>
            <p class="text-xs text-slate-400">
                Browse and override external ID mappings
            </p>
        </div>
        {#key searchBarKey}
            <SearchBar
                bind:query
                bind:customOnly
                bind:page
                {loading}
                onLoad={load}
                onCancel={cancelLoad}
                onCreate={openCreateEditor}
                onIncludes={openIncludesEditor}
                onSubmit={handleSearchSubmit} />
        {/key}
    </div>
    <div
        class="relative flex h-[70vh] flex-col overflow-hidden rounded-md border border-slate-800/70 bg-slate-900/40 backdrop-blur-sm">
        <div
            class="flex items-center gap-4 border-b border-slate-800/60 bg-slate-950/50 px-3 py-2 text-[11px]">
            <span class="text-slate-400"
                >Showing <span class="font-medium text-slate-200">{items.length}</span
                >/<span class="text-slate-500">{total}</span></span>
            {#if pages > 1}<span class="text-slate-500">Page {page} / {pages}</span
                >{/if}
            {#if customOnly}<span class="text-emerald-400">Custom overrides only</span
                >{/if}
            <span class="flex-1"></span>
            <button
                onclick={load}
                class="-mr-2 inline-flex h-6 w-6 items-center justify-center rounded bg-slate-800/50 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200"
                title="Refresh"
                aria-label="Refresh">
                <RefreshCcw class="h-4 w-4" />
            </button>
            <!-- Column settings popover -->
            <Popover.Root>
                <Popover.Trigger
                    class="inline-flex h-6 w-6 items-center justify-center rounded bg-slate-800/50 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200"
                    title="Column settings"
                    aria-label="Column settings">
                    <Eye class="h-4 w-4" />
                </Popover.Trigger>
                <Popover.Content
                    class="z-50 w-64 rounded-md border border-slate-700 bg-slate-900 p-3 shadow-lg"
                    side="bottom"
                    align="end"
                    sideOffset={4}>
                    <div class="flex items-center justify-between">
                        <h3 class="text-sm font-medium text-slate-200">
                            Column Visibility
                        </h3>
                        {#if columns.some((c) => !c.visible)}<button
                                onclick={showAllColumns}
                                class="text-xs text-slate-400 hover:text-slate-200"
                                >Show All</button>
                        {:else}
                            <button
                                onclick={hideAllColumns}
                                class="text-xs text-slate-400 hover:text-slate-200"
                                >Hide All</button>
                        {/if}
                    </div>
                    <div>
                        <button
                            onclick={resetColumns}
                            class="muted mb-3 text-xs text-slate-400 italic hover:text-slate-200">
                            (reset)
                        </button>
                    </div>
                    <div class="space-y-2">
                        {#each columns as column (column.id)}
                            <div class="flex items-center gap-2">
                                <label
                                    class="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
                                    <Checkbox.Root
                                        bind:checked={column.visible}
                                        class="flex h-4 w-4 items-center justify-center rounded border border-slate-600 bg-slate-800 data-[state=checked]:border-emerald-600 data-[state=checked]:bg-emerald-600">
                                        {#snippet children({ checked, indeterminate })}
                                            {#if indeterminate}
                                                <div
                                                    class="h-2 w-2 rounded-sm bg-white">
                                                </div>
                                            {:else if checked}
                                                <Check class="h-3 w-3 text-white" />
                                            {/if}
                                        {/snippet}
                                    </Checkbox.Root>
                                    <span class="truncate uppercase select-none"
                                        >{column.title}</span>
                                </label>
                            </div>
                        {/each}
                    </div>
                </Popover.Content>
            </Popover.Root>
        </div>
        <MappingsTable
            {items}
            bind:columns
            onEdit={handleEdit}
            onNavigateToQuery={({ query: next }) => navigateToQuery(next)} />
    </div>
    <Pagination
        class="mt-3"
        bind:page
        bind:perPage
        bind:pages
        onChange={load} />
</div>

<EditModal
    bind:open={editorOpen}
    mode={editorMode}
    mapping={editorTarget}
    onSaved={handleSaved} />

<IncludesModal
    bind:open={includesOpen}
    onSaved={handleIncludesSaved} />
