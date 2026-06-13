<script lang="ts">
    import { onMount, type Component } from "svelte";

    import {
        ExternalLink,
        LoaderCircle,
        Pin,
        PinOff,
        Plus,
        RefreshCcw,
        Search,
        SlidersHorizontal,
        Trash2,
        X,
    } from "@lucide/svelte";

    import PinFieldsEditor from "$lib/components/timeline/pin-fields-editor.svelte";
    import type {
        PinFieldOption,
        PinListResponse,
        PinResponse,
        PinSearchResponse,
        PinSearchResult,
        ProviderMediaMetadata,
    } from "$lib/types/api";
    import { apiFetch } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";
    import { clearPinOptionsCache, loadPinOptions } from "$lib/utils/pin-options";
    import { pinIdentifier } from "$lib/utils/provider-ref";

    interface Props {
        profile: string;
    }

    type RowKey = string;
    type RowSource = "pinned" | "search";

    interface PinRow {
        rowKey: RowKey;
        namespace: string;
        key: string;
        media: ProviderMediaMetadata | null;
        pin: PinResponse | null;
        source: RowSource;
    }

    let { profile }: Props = $props();

    let open = $state(false);
    let searchQuery = $state("");
    let searchResults: PinSearchResult[] = $state([]);
    let searchLoading = $state(false);
    let searchSubmitted = $state(false);
    let searchError: string | null = $state(null);

    let pinned: PinResponse[] = $state([]);
    let pinnedLoading = $state(false);
    let pinnedError: string | null = $state(null);

    let options: PinFieldOption[] = $state([]);
    let optionsLoading = $state(false);
    let optionsError: string | null = $state(null);

    let expanded: Record<RowKey, boolean> = $state({});
    let saving: Record<RowKey, boolean> = $state({});
    let rowError: Record<RowKey, string | null> = $state({});
    let selections: Record<RowKey, string[]> = $state({});
    let baselines: Record<RowKey, string[]> = $state({});

    const ROW_KEY_SEPARATOR = "::";

    const pinnedCount = $derived(pinned.length);
    const searchResultCount = $derived(searchResults.length);
    const activeSearchPins = $derived(
        searchResults.filter((result) => result.pin?.fields?.length).length,
    );

    const pinnedRows = $derived.by(() => {
        const rows: PinRow[] = [];
        for (const pin of pinned) {
            const id = pinIdentifier(pin);
            if (!id) continue;
            rows.push({
                rowKey: makeRowKey(id.namespace, id.key),
                namespace: id.namespace,
                key: id.key,
                media: pin.media ?? null,
                pin,
                source: "pinned" as const,
            });
        }
        return rows;
    });

    const searchRows = $derived.by((): PinRow[] =>
        searchResults.map((result) => ({
            rowKey: makeRowKey(result.media.namespace, result.media.key),
            namespace: result.media.namespace,
            key: result.media.key,
            media: result.media,
            pin: result.pin ?? null,
            source: "search" as const,
        })),
    );

    function makeRowKey(namespace: string, key: string): RowKey {
        return `${namespace}${ROW_KEY_SEPARATOR}${key}`;
    }

    function rowTitle(row: PinRow): string {
        return row.media?.title || `${row.namespace}:${row.key}`;
    }

    function fieldLabel(value: string): string {
        return options.find((option) => option.value === value)?.label ?? value;
    }

    function fieldSummary(fields: string[]): string {
        if (!fields.length) return "No fields pinned";
        return fields.map(fieldLabel).join(", ");
    }

    function rowFields(row: PinRow): string[] {
        return selections[row.rowKey] ?? row.pin?.fields ?? [];
    }

    function setRow(key: RowKey, fields: string[], updateBaseline = false) {
        selections[key] = [...fields];
        if (updateBaseline) baselines[key] = [...fields];
    }

    function openEditor(row: PinRow) {
        const fields = rowFields(row);
        setRow(row.rowKey, fields, false);
        expanded[row.rowKey] = true;
    }

    async function ensureOptions(force = false) {
        if (options.length && !force) return;
        optionsLoading = true;
        optionsError = null;
        try {
            options = [...(await loadPinOptions(force))];
        } catch (e) {
            console.error(e);
            optionsError = (e as Error)?.message || "Failed to load pin options";
        } finally {
            optionsLoading = false;
        }
    }

    async function loadPinned() {
        pinnedLoading = true;
        pinnedError = null;
        try {
            const response = await apiFetch(`/api/pins/${profile}?with_media=true`);
            if (!response.ok) throw new Error("HTTP " + response.status);
            const data = (await response.json()) as PinListResponse;
            pinned = data.pins || [];
            for (const pin of pinned) {
                const id = pinIdentifier(pin);
                if (!id) continue;
                setRow(makeRowKey(id.namespace, id.key), pin.fields || [], true);
            }
        } catch (e) {
            console.error(e);
            pinnedError = (e as Error)?.message || "Failed to load pins";
            toast("Failed to load pins", "error");
        } finally {
            pinnedLoading = false;
        }
    }

    async function searchTarget() {
        const query = searchQuery.trim();
        if (!query || searchLoading) return;
        searchLoading = true;
        searchSubmitted = true;
        searchError = null;
        try {
            await ensureOptions(false);
            const response = await apiFetch(
                `/api/pins/${profile}/search?q=${encodeURIComponent(query)}&limit=12`,
            );
            if (!response.ok) throw new Error("HTTP " + response.status);
            const data = (await response.json()) as PinSearchResponse;
            searchResults = data.results || [];
            for (const result of searchResults) {
                const key = makeRowKey(result.media.namespace, result.media.key);
                const current = selections[key];
                setRow(key, result.pin?.fields || current || [], true);
            }
        } catch (e) {
            console.error(e);
            searchError = (e as Error)?.message || "Failed to search target";
            toast("Failed to search target", "error");
        } finally {
            searchLoading = false;
        }
    }

    async function saveRow(row: PinRow, fields: string[]) {
        if (saving[row.rowKey]) return;
        saving[row.rowKey] = true;
        rowError[row.rowKey] = null;
        try {
            if (!fields.length) {
                const response = await apiFetch(
                    `/api/pins/${profile}/${encodeURIComponent(row.key)}`,
                    { method: "DELETE" },
                );
                if (!response.ok) throw new Error("HTTP " + response.status);
                setRow(row.rowKey, [], true);
                removePinned(row);
                patchSearchPin(row, null);
                toast("Pins cleared", "success");
                return;
            }

            const response = await apiFetch(
                `/api/pins/${profile}/${encodeURIComponent(row.key)}?with_media=true`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ fields }),
                },
                { successMessage: "Pins updated" },
            );
            if (!response.ok) throw new Error("HTTP " + response.status);
            const saved = (await response.json()) as PinResponse;
            const next = saved.fields || [];
            const merged = mergePin(row, saved);
            setRow(row.rowKey, next, true);
            upsertPinned(row, merged);
            patchSearchPin(row, merged);
        } catch (e) {
            console.error(e);
            rowError[row.rowKey] = (e as Error)?.message || "Failed to save";
            toast("Failed to save pins", "error");
        } finally {
            saving[row.rowKey] = false;
        }
    }

    function mergePin(row: PinRow, saved: PinResponse): PinResponse {
        return {
            ...(row.pin ?? {}),
            ...saved,
            media: saved.media ?? row.media ?? row.pin?.media ?? null,
        };
    }

    function upsertPinned(row: PinRow, pin: PinResponse) {
        const idx = pinned.findIndex((item) => {
            const id = pinIdentifier(item);
            return id?.namespace === row.namespace && id.key === row.key;
        });
        if (idx >= 0) pinned[idx] = pin;
        else pinned = [pin, ...pinned];
    }

    function removePinned(row: PinRow) {
        pinned = pinned.filter((item) => {
            const id = pinIdentifier(item);
            return !(id?.namespace === row.namespace && id.key === row.key);
        });
    }

    function patchSearchPin(row: PinRow, pin: PinResponse | null) {
        searchResults = searchResults.map((result) =>
            result.media.namespace === row.namespace && result.media.key === row.key
                ? { ...result, pin }
                : result,
        );
    }

    function togglePanel() {
        open = !open;
        if (open) {
            void ensureOptions(false);
            void loadPinned();
        }
    }

    function refreshAll() {
        clearPinOptionsCache();
        void ensureOptions(true);
        void loadPinned();
        if (searchSubmitted && searchQuery.trim()) void searchTarget();
    }

    $effect(() => {
        if (!open) {
            expanded = {};
            saving = {};
            rowError = {};
        }
    });

    onMount(() => {
        void ensureOptions(false);
    });
</script>

{#snippet EmptyState(icon: Component, title: string, detail: string)}
    {@const Icon = icon}
    <div class="flex items-center gap-3 px-3 py-4 text-slate-400">
        <Icon class="h-4 w-4 text-slate-500" />
        <div>
            <div class="text-[11px] font-semibold text-slate-300">{title}</div>
            <div class="text-[11px]">{detail}</div>
        </div>
    </div>
{/snippet}

{#snippet PinRowView(row: PinRow)}
    {@const fields = rowFields(row)}
    <div class="border-b border-slate-800/60 last:border-b-0">
        <div
            class="grid gap-3 px-3 py-2.5 transition-colors hover:bg-slate-800/30 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
            <div class="flex min-w-0 items-center gap-3">
                <div
                    class="grid h-14 w-10 shrink-0 place-items-center overflow-hidden rounded-md border border-slate-800 bg-slate-800/40 text-[9px] text-slate-500 select-none">
                    {#if row.media?.poster_url}
                        <img
                            src={row.media.poster_url}
                            alt=""
                            class="h-full w-full object-cover" />
                    {:else}
                        No Art
                    {/if}
                </div>
                <div class="min-w-0 flex-1">
                    <div class="flex min-w-0 items-center gap-2">
                        <div class="truncate text-[13px] font-semibold text-slate-100">
                            {rowTitle(row)}
                        </div>
                        {#if row.pin?.fields?.length}
                            <span
                                class="inline-flex shrink-0 rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-fuchsia-100 uppercase">
                                pinned
                            </span>
                        {/if}
                    </div>
                    <div
                        class="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-slate-400">
                        <span class="truncate font-mono text-slate-500">
                            {row.namespace}:{row.key}
                        </span>
                        {#if row.media?.external_url}
                            <!-- eslint-disable svelte/no-navigation-without-resolve -->
                            <a
                                href={row.media.external_url}
                                target="_blank"
                                rel="noopener"
                                class="inline-flex items-center gap-1 text-sky-400 hover:text-sky-300"
                                aria-label={`Open external ${row.key}`}
                                title={`Open external ${row.key}`}>
                                <ExternalLink class="h-3 w-3" />
                            </a>
                            <!-- eslint-enable svelte/no-navigation-without-resolve -->
                        {/if}
                    </div>
                </div>
            </div>

            <div class="flex items-center gap-2 md:justify-end">
                <button
                    type="button"
                    class="inline-flex h-7 items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-2 text-[11px] font-medium text-slate-100 hover:border-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
                    title={fields.length ? "Edit pinned fields" : "Add pinned fields"}
                    disabled={!!optionsError || saving[row.rowKey]}
                    onclick={() => openEditor(row)}>
                    {#if fields.length}
                        <SlidersHorizontal class="h-3.5 w-3.5" />
                        Edit
                    {:else}
                        <Plus class="h-3.5 w-3.5" />
                        Add
                    {/if}
                </button>
                {#if fields.length}
                    <button
                        type="button"
                        class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-700 bg-slate-900/40 text-slate-300 hover:border-red-500/60 hover:text-red-100 disabled:cursor-not-allowed disabled:opacity-50"
                        title="Clear pins"
                        disabled={saving[row.rowKey]}
                        onclick={() => void saveRow(row, [])}>
                        {#if saving[row.rowKey]}
                            <LoaderCircle class="h-3.5 w-3.5 animate-spin" />
                        {:else}
                            <Trash2 class="h-3.5 w-3.5" />
                        {/if}
                    </button>
                {/if}
            </div>
        </div>

        {#if expanded[row.rowKey]}
            <div class="border-t border-slate-800 bg-slate-950/60 px-3 pb-3">
                <div class="flex justify-end pt-2">
                    <button
                        type="button"
                        class="inline-flex items-center gap-1 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-sky-300 hover:bg-slate-700"
                        onclick={() => (expanded[row.rowKey] = false)}
                        title="Close pinned fields editor">
                        <X class="h-3.5 w-3.5" />
                        Close
                    </button>
                </div>
                <PinFieldsEditor
                    value={fields}
                    baseline={baselines[row.rowKey] ?? row.pin?.fields ?? []}
                    {options}
                    loading={optionsLoading}
                    saving={saving[row.rowKey] || false}
                    error={rowError[row.rowKey] || null}
                    {optionsError}
                    disabled={!!optionsError}
                    title="Pinned fields"
                    subtitle={fieldSummary(fields)}
                    showRefresh={false}
                    onSave={(value) => saveRow(row, value)}
                    onChange={(value) => (selections[row.rowKey] = [...value])}
                    onRefresh={(force) => ensureOptions(force)} />
            </div>
        {/if}
    </div>
{/snippet}

<div class="relative inline-flex items-center gap-2">
    <button
        type="button"
        class="inline-flex items-center gap-1 rounded-md border border-fuchsia-600/50 bg-fuchsia-600/20 py-1 pr-2 pl-2 text-[12px] font-medium text-fuchsia-100 hover:bg-fuchsia-600/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-fuchsia-400/60"
        aria-expanded={open}
        aria-controls="global-pins-panel"
        title={open ? "Hide pins manager" : "Show pins manager"}
        onclick={togglePanel}>
        <Pin class="inline h-4 w-4" />
        <span class="hidden sm:inline">Pins</span>
        <span
            class="ml-1 inline-flex h-5 min-w-5 items-center justify-center rounded border border-white/10 bg-fuchsia-700/30 px-1 text-[10px] font-semibold text-white/90">
            {pinnedCount}
        </span>
    </button>
    <button
        type="button"
        class="inline-flex items-center gap-1 rounded-md border border-slate-600/60 bg-slate-700/40 px-2 py-1 text-[11px] font-medium text-slate-200 hover:bg-slate-600/50 disabled:cursor-not-allowed disabled:opacity-50"
        title="Refresh pins and options"
        disabled={!open || pinnedLoading || optionsLoading}
        onclick={refreshAll}>
        <RefreshCcw
            class={`h-3.5 w-3.5 ${pinnedLoading || optionsLoading ? "animate-spin" : ""}`} />
        <span class="hidden md:inline">Refresh</span>
    </button>
</div>

{#if open}
    <section
        id="global-pins-panel"
        aria-label="Global pins manager"
        class="mt-2 overflow-hidden rounded-md border border-slate-800 bg-slate-900/60 p-3 shadow-sm backdrop-blur-sm">
        <div>
            <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <div
                        class="flex items-center gap-2 text-[10px] font-semibold tracking-wide text-slate-100 uppercase">
                        <Pin class="h-3.5 w-3.5 text-slate-300" />
                        Pins Manager
                    </div>
                    <div class="mt-1 text-[11px] text-slate-400">
                        {pinnedCount} pinned · {activeSearchPins} pinned in search results
                    </div>
                </div>
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-sky-300 hover:bg-slate-700"
                    onclick={togglePanel}>
                    <X class="h-3.5 w-3.5" />
                    Close
                </button>
            </div>

            <form
                class="mt-3 grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]"
                onsubmit={(event) => {
                    event.preventDefault();
                    void searchTarget();
                }}>
                <label class="relative min-w-0">
                    <Search
                        class="pointer-events-none absolute top-1/2 left-2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                    <input
                        class="h-8 w-full rounded-md border border-slate-700 bg-slate-950/80 py-1 pr-3 pl-7 text-[12px] text-slate-100 outline-none placeholder:text-slate-500 focus:border-sky-500"
                        placeholder="Search the target provider"
                        value={searchQuery}
                        oninput={(event) =>
                            (searchQuery = (event.currentTarget as HTMLInputElement)
                                .value)}
                        disabled={searchLoading} />
                </label>
                <button
                    type="submit"
                    class="inline-flex h-8 items-center justify-center gap-1 rounded-md border border-sky-600/60 bg-sky-700/50 px-3 text-[11px] font-semibold text-sky-100 hover:bg-sky-600/60 disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={!searchQuery.trim() || searchLoading}>
                    {#if searchLoading}
                        <LoaderCircle class="h-3.5 w-3.5 animate-spin" />
                    {:else}
                        <Search class="h-3.5 w-3.5" />
                    {/if}
                    Search
                </button>
            </form>
        </div>

        {#if optionsError}
            <div
                class="mt-3 rounded-md border border-amber-600/60 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-100">
                <div class="flex flex-wrap items-center gap-2">
                    <span class="font-semibold">{optionsError}</span>
                    <button
                        type="button"
                        class="inline-flex h-7 items-center gap-1 rounded-md border border-amber-500/70 px-2 text-[11px] hover:border-amber-400"
                        onclick={() => ensureOptions(true)}>
                        <RefreshCcw class="h-3.5 w-3.5" />
                        Retry
                    </button>
                </div>
            </div>
        {/if}

        <div
            class="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)]">
            <section
                class="overflow-hidden rounded-md border border-slate-800 bg-slate-950/80 will-change-transform">
                <div
                    class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
                    <div class="flex items-center gap-2">
                        <Search class="h-3.5 w-3.5 text-slate-300" />
                        <span
                            class="text-[10px] font-semibold tracking-wide text-slate-100 uppercase">
                            Search results
                        </span>
                        {#if searchSubmitted}
                            <span class="text-[11px] text-slate-500"
                                >{searchResultCount}</span>
                        {/if}
                    </div>
                    {#if searchLoading}
                        <span
                            class="inline-flex items-center gap-1 text-[11px] text-sky-300">
                            <LoaderCircle class="h-3.5 w-3.5 animate-spin" />
                            Searching
                        </span>
                    {/if}
                </div>

                <div class="max-h-[calc(100vh-18rem)] min-h-0 overflow-y-auto">
                    {#if searchError}
                        <div class="px-4 py-3 text-[12px] text-red-200">
                            {searchError}
                        </div>
                    {:else if searchRows.length}
                        <div>
                            {#each searchRows as row (row.rowKey)}
                                {@render PinRowView(row)}
                            {/each}
                        </div>
                    {:else if searchSubmitted && !searchLoading}
                        {@render EmptyState(
                            PinOff,
                            "No matches",
                            "Try a different target-provider title search.",
                        )}
                    {:else}
                        {@render EmptyState(
                            Search,
                            "Search target entries",
                            "Find entries that are not in your current pins yet.",
                        )}
                    {/if}
                </div>
            </section>

            <section
                class="overflow-hidden rounded-md border border-slate-800 bg-slate-950/80 will-change-transform">
                <div
                    class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
                    <div class="flex items-center gap-2">
                        <Pin class="h-3.5 w-3.5 text-slate-300" />
                        <span
                            class="text-[10px] font-semibold tracking-wide text-slate-100 uppercase">
                            Pinned entries
                        </span>
                        <span class="text-[11px] text-slate-500">{pinnedCount}</span>
                    </div>
                    {#if pinnedLoading}
                        <span
                            class="inline-flex items-center gap-1 text-[11px] text-sky-300">
                            <LoaderCircle class="h-3.5 w-3.5 animate-spin" />
                            Loading
                        </span>
                    {/if}
                </div>

                <div class="max-h-[calc(100vh-18rem)] min-h-0 overflow-y-auto">
                    {#if pinnedError}
                        <div class="px-4 py-3 text-[12px] text-red-200">
                            {pinnedError}
                        </div>
                    {:else if pinnedRows.length}
                        <div>
                            {#each pinnedRows as row (row.rowKey)}
                                {@render PinRowView(row)}
                            {/each}
                        </div>
                    {:else if !pinnedLoading}
                        {@render EmptyState(
                            PinOff,
                            "No pinned entries",
                            "Search for target entries and pin fields from the results.",
                        )}
                    {/if}
                </div>
            </section>
        </div>
    </section>
{/if}
