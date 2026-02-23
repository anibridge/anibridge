<script lang="ts">
    import { onMount } from "svelte";

    import { LoaderCircle, Pin, RefreshCcw, X } from "@lucide/svelte";

    import PinFieldsEditor from "$lib/components/timeline/pin-fields-editor.svelte";
    import TimelineItem, {
        type PinsPanelContext,
    } from "$lib/components/timeline/timeline-item.svelte";
    import type { OutcomeMeta } from "$lib/components/timeline/types";
    import type {
        HistoryItem,
        PinFieldOption,
        PinListResponse,
        PinResponse,
        ProviderMediaMetadata,
    } from "$lib/types/api";
    import { apiFetch } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";
    import { clearPinOptionsCache, loadPinOptions } from "$lib/utils/pin-options";

    interface Props {
        profile: string;
    }
    let { profile }: Props = $props();

    // Panel open state
    let open = $state(false);

    // Shared pin options
    let options: PinFieldOption[] = $state([]);
    let optionsLoading = $state(false);
    let optionsError: string | null = $state(null);

    // Pinned list
    let pinned: PinResponse[] = $state([]);
    let pinnedLoading = $state(false);
    let pinnedError: string | null = $state(null);

    // Per-row editor state
    type RowKey = string;
    const ROW_KEY_SEPARATOR = "::";

    const makeRowKey = (namespace?: string | null, mediaKey?: string | null) => {
        if (!namespace || !mediaKey) return null;
        return `${namespace}${ROW_KEY_SEPARATOR}${mediaKey}`;
    };

    const parseRowKey = (key: RowKey) => {
        const idx = key.indexOf(ROW_KEY_SEPARATOR);
        if (idx === -1) return { namespace: "", mediaKey: key };
        return {
            namespace: key.slice(0, idx),
            mediaKey: key.slice(idx + ROW_KEY_SEPARATOR.length),
        };
    };

    let rowIds: Record<RowKey, number> = $state({});
    let nextRowId = $state(1);

    function ensureRowId(key: RowKey): number {
        const existing = rowIds[key];
        if (existing) return existing;
        const assigned = nextRowId;
        nextRowId = assigned + 1;
        rowIds[key] = assigned;
        return assigned;
    }

    let expanded: Record<RowKey, boolean> = $state({});
    let saving: Record<RowKey, boolean> = $state({});
    let rowError: Record<RowKey, string | null> = $state({});
    let selections: Record<RowKey, string[]> = $state({});
    let baselines: Record<RowKey, string[]> = $state({});

    const PINNED_META: OutcomeMeta = {
        label: "Pinned",
        color: "bg-fuchsia-700/60",
        icon: Pin,
        order: 0,
    };

    interface PinsPanelData {
        value: string[];
        baseline: string[];
        options: PinFieldOption[];
        optionsLoading: boolean;
        saving: boolean;
        error: string | null;
        optionsError: string | null;
        disabled: boolean;
        onSave: (value: string[]) => void;
        onChange: (value: string[]) => void;
        onRefresh: (force: boolean) => void;
    }

    const timelineDisplayTitle = (item: HistoryItem): string | null =>
        item.list_media?.title ??
        item.library_media?.title ??
        (item.list_namespace && item.list_media_key
            ? `${item.list_namespace}:${item.list_media_key}`
            : null) ??
        (item.library_namespace && item.library_media_key
            ? `${item.library_namespace}:${item.library_media_key}`
            : null) ??
        "Unknown";

    const timelineCoverImage = (item: HistoryItem): string | null =>
        item.list_media?.poster_url ?? item.library_media?.poster_url ?? null;

    function toHistoryItem(
        key: RowKey,
        pin: PinResponse | null | undefined,
        media: ProviderMediaMetadata | null,
        outcome: string,
    ): HistoryItem {
        const { namespace, mediaKey } = parseRowKey(key);
        const rowId = ensureRowId(key);
        const timestamp =
            pin?.updated_at || pin?.created_at || new Date().toISOString();
        const resolvedMedia = media ?? pin?.media ?? null;
        return {
            id: rowId,
            profile_name: profile,
            library_namespace: null,
            library_section_key: null,
            library_media_key: null,
            list_namespace: namespace || null,
            list_media_key: mediaKey || null,
            media_kind: null,
            outcome,
            before_state: null,
            after_state: null,
            info: null,
            error_message: null,
            timestamp,
            library_media: null,
            list_media: resolvedMedia,
            pinned_fields: pin?.fields ?? [],
        } satisfies HistoryItem;
    }

    function setRow(key: RowKey | null, fields: string[], updateBaseline = false) {
        if (!key) return;
        selections[key] = [...fields];
        if (updateBaseline) baselines[key] = [...fields];
    }

    async function ensureOptions(force = false) {
        if (options.length && !force) return;
        optionsLoading = true;
        optionsError = null;
        try {
            const loaded = await loadPinOptions(force);
            options = [...loaded];
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
            const r = await apiFetch(`/api/pins/${profile}?with_media=true`);
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = (await r.json()) as PinListResponse;
            pinned = d.pins || [];
            for (const entry of pinned) {
                const key = makeRowKey(entry.list_namespace, entry.list_media_key);
                if (key) ensureRowId(key);
                setRow(key, entry.fields || [], true);
            }
        } catch (e) {
            console.error(e);
            pinnedError = (e as Error)?.message || "Failed to load pins";
            toast("Failed to load pins", "error");
        } finally {
            pinnedLoading = false;
        }
    }

    async function save(
        rowKey: RowKey,
        namespace: string,
        mediaKey: string,
        fields: string[],
        metadata: ProviderMediaMetadata | null,
    ) {
        if (saving[rowKey]) return;
        saving[rowKey] = true;
        rowError[rowKey] = null;
        try {
            if (!fields.length) {
                const r = await apiFetch(`/api/pins/${profile}/${mediaKey}`, {
                    method: "DELETE",
                });
                if (!r.ok) throw new Error("HTTP " + r.status);
                setRow(rowKey, [], true);
                pinned = pinned.filter(
                    (p) =>
                        !(
                            p.list_namespace === namespace &&
                            p.list_media_key === mediaKey
                        ),
                );
                toast("Pins cleared", "success");
                return;
            }
            const r = await apiFetch(
                `/api/pins/${profile}/${mediaKey}?with_media=true`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ fields }),
                },
                { successMessage: "Pins updated" },
            );
            if (!r.ok) throw new Error("HTTP " + r.status);
            const d = (await r.json()) as PinResponse;
            const next = d.fields || [];
            setRow(rowKey, next, true);

            const idx = pinned.findIndex(
                (p) => p.list_namespace === namespace && p.list_media_key === mediaKey,
            );
            const existing = idx >= 0 ? pinned[idx] : null;
            const resolvedMedia = d.media ?? metadata ?? existing?.media ?? null;
            const merged: PinResponse = {
                ...(existing ?? {}),
                ...d,
                media: resolvedMedia,
            };

            if (idx >= 0) pinned[idx] = merged;
            else pinned = [merged, ...pinned];
        } catch (e) {
            console.error(e);
            rowError[rowKey] = (e as Error)?.message || "Failed to save";
            toast("Failed to save pins", "error");
        } finally {
            saving[rowKey] = false;
        }
    }

    function togglePanel() {
        const next = !open;
        open = next;
        if (next) {
            void ensureOptions(false);
            void loadPinned();
        }
    }

    function refreshAll() {
        clearPinOptionsCache();
        void ensureOptions(true);
        void loadPinned();
    }

    function toggleRow(key: RowKey) {
        expanded[key] = !expanded[key];
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

    function panelDataFor(
        key: RowKey,
        base: string[],
        sel: string[],
        namespace: string,
        mediaKey: string,
        metadata: ProviderMediaMetadata | null,
    ): PinsPanelData {
        return {
            value: sel,
            baseline: baselines[key] ?? base,
            options,
            optionsLoading,
            saving: saving[key] || false,
            error: rowError[key] || null,
            optionsError,
            disabled: !!optionsError,
            onSave: (value: string[]) =>
                save(key, namespace, mediaKey, value, metadata),
            onChange: (value: string[]) => (selections[key] = [...value]),
            onRefresh: (force: boolean) => ensureOptions(force),
        };
    }
</script>

{#snippet PinEditorPanel(props: PinsPanelContext)}
    {@const data = props.data as PinsPanelData | undefined}
    {#if props.openPins && data}
        <PinFieldsEditor
            value={data.value}
            baseline={data.baseline}
            options={data.options}
            loading={data.optionsLoading}
            saving={data.saving}
            error={data.error}
            optionsError={data.optionsError}
            disabled={data.disabled}
            title="Pin fields"
            subtitle="Keep these fields unchanged for this entry when syncing."
            onSave={data.onSave}
            onChange={data.onChange}
            onRefresh={data.onRefresh} />
    {/if}
{/snippet}

<div class="relative inline-flex items-center gap-2">
    <button
        type="button"
        class="inline-flex items-center gap-2 rounded-md border border-fuchsia-600/50 bg-fuchsia-600/20 py-1 pr-2 pl-2 text-[12px] font-medium text-fuchsia-100 hover:bg-fuchsia-600/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-fuchsia-400/60"
        aria-expanded={open}
        aria-controls="global-pins-panel"
        title={open ? "Hide pins manager" : "Show pins manager"}
        onclick={togglePanel}>
        <Pin class="inline h-4 w-4" />
        <span class="hidden sm:inline">Pins</span>
        <span
            class="ml-1 inline-flex h-5 min-w-5 items-center justify-center rounded border border-white/10 bg-fuchsia-700/30 px-1 text-[10px] font-semibold text-white/90">
            {pinned.length}
        </span>
    </button>
    <button
        type="button"
        class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1 text-[11px] text-slate-100 hover:border-slate-600 disabled:opacity-60"
        title="Refresh pins and options"
        disabled={!open}
        onclick={refreshAll}>
        <RefreshCcw class="inline h-3.5 w-3.5" />
        <span class="hidden md:inline">Refresh</span>
    </button>
</div>

{#if open}
    <section
        id="global-pins-panel"
        aria-label="Global pins manager"
        class="mt-2 overflow-hidden rounded-lg border border-slate-800 bg-slate-950/70 shadow-md shadow-black/30">
        <div class="px-3 pt-2 pb-3 text-[11px]">
            {#if optionsError}
                <div
                    class="mb-3 rounded-md border border-amber-600/60 bg-amber-900/20 px-3 py-2 text-amber-100">
                    <div class="mb-1 font-semibold">{optionsError}</div>
                    <button
                        class="inline-flex items-center gap-1 rounded-md border border-amber-500/70 px-2.5 py-1 hover:border-amber-400"
                        onclick={() => ensureOptions(true)}>
                        <RefreshCcw class="h-3.5 w-3.5" /> Retry loading options
                    </button>
                </div>
            {/if}

            <!-- Pinned list -->
            <div
                class="mb-4 overflow-hidden rounded-md border border-slate-800 bg-slate-950/70">
                <div
                    class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
                    <div class="flex items-center gap-2 text-[10px]">
                        <Pin class="h-3.5 w-3.5" />
                        <span
                            class="font-semibold tracking-wide text-slate-100 uppercase"
                            >Pinned entries</span>
                        <span class="text-[11px] font-normal text-slate-400 normal-case"
                            >{pinned.length}</span>
                    </div>
                    <div class="text-[11px] text-slate-400">
                        {#if pinnedLoading}
                            <span class="inline-flex items-center gap-1 text-sky-300"
                                ><LoaderCircle
                                    class="inline h-3.5 w-3.5 animate-spin" /> Loading…</span>
                        {/if}
                    </div>
                </div>
                <div class="divide-y divide-slate-800">
                    {#if pinnedError}
                        <div class="px-3 py-2 text-red-200">{pinnedError}</div>
                    {:else if !pinnedLoading && !pinned.length}
                        <div class="px-3 py-2 text-slate-400">No pinned entries.</div>
                    {/if}
                    {#each pinned as p (p.profile_name + ":" + (p.list_namespace ?? "") + ":" + (p.list_media_key ?? ""))}
                        {@const rowKey = makeRowKey(p.list_namespace, p.list_media_key)}
                        {@const base = p.fields || []}
                        {@const sel = rowKey ? (selections[rowKey] ?? base) : base}
                        {#if rowKey}
                            {@const identifiers = parseRowKey(rowKey)}
                            {@const historyItem = toHistoryItem(
                                rowKey,
                                p,
                                p.media ?? null,
                                "pinned",
                            )}
                            {@const panelData = panelDataFor(
                                rowKey,
                                base,
                                sel,
                                identifiers.namespace,
                                identifiers.mediaKey,
                                p.media ?? null,
                            )}
                            <div class="px-3 py-2">
                                <TimelineItem
                                    {profile}
                                    item={historyItem}
                                    meta={PINNED_META}
                                    displayTitle={timelineDisplayTitle}
                                    coverImage={timelineCoverImage}
                                    hasPins={true}
                                    togglePins={() => toggleRow(rowKey)}
                                    openPins={expanded[rowKey] || false}
                                    pinButtonLoading={saving[rowKey] || false}
                                    pinButtonDisabled={!!optionsError}
                                    pinCount={sel.length}
                                    pinsPanel={PinEditorPanel}
                                    pinsPanelData={panelData} />
                            </div>
                        {:else}
                            <div class="px-3 py-2 text-[11px] text-amber-200">
                                Missing provider identifiers for pinned entry.
                            </div>
                        {/if}
                    {/each}
                </div>
            </div>

            <div
                class="mt-2 flex items-center justify-between gap-2 border-t border-slate-800 bg-slate-950/60 px-3 py-2 text-[11px] text-slate-400">
                <div class="flex items-center gap-3">
                    <span class="mr-2">{pinned.length} pinned</span>
                    {#if optionsLoading}
                        <span class="inline-flex items-center gap-1 text-sky-300"
                            ><LoaderCircle class="inline h-3.5 w-3.5 animate-spin" /> options…</span>
                    {/if}
                </div>
                <div class="ml-auto">
                    <button
                        type="button"
                        class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1 hover:border-slate-600"
                        onclick={togglePanel}>
                        <X class="inline h-3.5 w-3.5" />
                        Close
                    </button>
                </div>
            </div>
        </div>
    </section>
{/if}
