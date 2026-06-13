<script lang="ts">
    import type { Snippet } from "svelte";

    import {
        ExternalLink,
        Hash,
        Info,
        LoaderCircle,
        SquareMinus,
        SquarePlus,
        Trash2,
    } from "@lucide/svelte";
    import { SvelteURLSearchParams } from "svelte/reactivity";
    import { fade } from "svelte/transition";

    import { resolve } from "$app/paths";
    import TimelineDiffViewer from "$lib/components/timeline/timeline-diff-viewer.svelte";
    import TimelineManagePins from "$lib/components/timeline/timeline-manage-pins.svelte";
    import type { ItemDiffUi, OutcomeMeta } from "$lib/components/timeline/types";
    import type { HistoryItem } from "$lib/types/api";
    import { titleCase } from "$lib/utils/text";

    export interface PinsPanelContext {
        item: HistoryItem;
        profile: string;
        openPins: boolean;
        pinCount?: number;
        pinButtonLoading: boolean;
        pinButtonDisabled: boolean;
        togglePins?: (item: HistoryItem) => void;
        data?: unknown;
    }

    interface Props {
        item: HistoryItem;
        meta: OutcomeMeta;
        displayTitle: (item: HistoryItem) => string | null;
        coverImage: (item: HistoryItem) => string | null;
        deleteHistory?: (item: HistoryItem) => void;
        canShowDiff?: (item: HistoryItem) => boolean;
        toggleDiff?: (id: number) => void;
        openDiff?: boolean;
        ensureDiffUi?: (id: number) => ItemDiffUi;
        diffCount?: number;
        hasPins?: boolean;
        togglePins?: (item: HistoryItem) => void;
        openPins?: boolean;
        pinButtonLoading?: boolean;
        pinButtonDisabled?: boolean;
        pinCount?: number;
        profile: string;
        onPinsDraft?: (item: HistoryItem, fields: string[]) => void;
        onPinsSaved?: (item: HistoryItem, fields: string[]) => void;
        onPinsBusy?: (item: HistoryItem, value: boolean) => void;
        pinsPanel?: Snippet<[PinsPanelContext]>;
        pinsPanelData?: unknown;
    }

    let {
        item,
        meta,
        displayTitle,
        coverImage,
        deleteHistory,
        canShowDiff = () => false,
        toggleDiff = () => undefined,
        openDiff = false,
        ensureDiffUi,
        diffCount,
        hasPins = false,
        togglePins,
        openPins = false,
        pinButtonLoading = false,
        pinButtonDisabled = false,
        pinCount,
        profile,
        onPinsDraft,
        onPinsSaved,
        onPinsBusy,
        pinsPanel,
        pinsPanelData,
    }: Props = $props();

    function mappingUrl(item: HistoryItem): string | null {
        if (!item.animap_provider || !item.animap_id) return null;
        const parts = [
            `source.provider:${item.animap_provider}`,
            `source.id:${item.animap_id}`,
        ];
        if (item.animap_scope) parts.push(`source.scope:${item.animap_scope}`);
        return (
            resolve("/mappings") +
            "?" +
            new SvelteURLSearchParams({ q: parts.join(" ") }).toString()
        );
    }

    const fallbackDiffUi: ItemDiffUi = {
        tab: "changes",
        filter: "",
        showUnchanged: false,
    };

    const ui = () => ensureDiffUi?.(item.id) ?? fallbackDiffUi;

    function formatTimestamp(raw?: string): string | null {
        if (!raw) return null;
        const normalized = raw.endsWith("Z") || raw.endsWith("z") ? raw : `${raw}Z`;
        const value = new Date(normalized);
        if (Number.isNaN(value.getTime())) return null;
        return value.toLocaleString();
    }

    const timestampLabel = $derived(formatTimestamp(item.timestamp));
    let openInfo = $state(false);
    const infoEntries = $derived.by(() =>
        Object.entries(item.info ?? {})
            .map(([key, value]) => [key.trim(), String(value ?? "").trim()] as const)
            .filter(([key, value]) => key.length > 0 && value.length > 0)
            .sort(([left], [right]) => left.localeCompare(right)),
    );
    const hasInfo = $derived(infoEntries.length > 0);
    const isEphemeral = $derived(Boolean(item.ephemeral));

    $effect(() => {
        if (!hasInfo) openInfo = false;
    });

    function formatInfoKey(key: string): string {
        return titleCase(key.replaceAll("_", " ").replaceAll("-", " "));
    }
</script>

{#snippet DefaultPins(props: PinsPanelContext)}
    <TimelineManagePins
        profile={props.profile}
        item={props.item}
        onDraft={(fields) => onPinsDraft?.(props.item, fields)}
        onSaved={(fields) => onPinsSaved?.(props.item, fields)}
        onBusy={(value) => onPinsBusy?.(props.item, value)} />
{/snippet}

<div
    class={`group relative flex items-stretch gap-3 overflow-hidden rounded-md border border-slate-800 bg-slate-900/60 p-3 shadow-sm backdrop-blur-sm transition-shadow hover:shadow-md sm:p-4 ${
        isEphemeral ? " opacity-80" : ""
    }`}>
    <div
        class={`w-1 shrink-0 self-stretch rounded-md ${meta.color}`}
        aria-hidden="true">
    </div>
    <div class="flex min-w-0 flex-1 items-stretch gap-3">
        {#if coverImage(item)}
            <div class="timeline-cover shrink-0 self-start">
                <div
                    class="timeline-cover-frame relative h-full w-full overflow-hidden rounded-md border border-slate-800 bg-slate-800/40">
                    <img
                        src={coverImage(item)!}
                        alt={displayTitle(item) || "Cover"}
                        loading="lazy"
                        class="timeline-cover-img h-full w-full object-cover transition-[filter] duration-150 ease-out group-hover:blur-none" />
                </div>
            </div>
        {:else}
            <div
                class="timeline-cover timeline-cover-fallback flex shrink-0 items-center justify-center self-start rounded-md border border-dashed border-slate-700 bg-slate-800/30 text-[9px] text-slate-500 select-none">
                No Art
            </div>
        {/if}
        <div class="flex min-w-0 flex-1 flex-col overflow-hidden">
            <header class="flex items-start gap-2">
                <div class="min-w-0 flex-1 space-y-1.5">
                    <div class="flex items-center gap-x-2 gap-y-1 whitespace-nowrap">
                        <span
                            class="text-sm font-semibold text-slate-100 sm:text-base"
                            title={displayTitle(item)}>
                            {displayTitle(item)}
                        </span>
                        <span
                            class={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium tracking-wide ${meta.color}`}>
                            <meta.icon class="inline h-3.5 w-3.5 text-[10px]" />
                            {meta.label}
                        </span>
                        {#if isEphemeral}
                            <span
                                class="inline-flex items-center gap-1 rounded-md border border-amber-400/70 bg-amber-400/20 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-amber-100 shadow-[0_0_0_1px_rgba(251,191,36,0.12)]">
                                Dry Run
                            </span>
                        {/if}
                    </div>
                    <div
                        class="chip-scroll space-y-1 overflow-x-auto mask-[linear-gradient(to_right,black,black_calc(100%-10px),transparent)] whitespace-nowrap">
                        <div class="flex items-center gap-1 text-[10px] text-slate-300">
                            {#if item.target_media?.external_url}
                                <!-- eslint-disable svelte/no-navigation-without-resolve -->
                                <a
                                    href={item.target_media.external_url}
                                    target="_blank"
                                    rel="noopener"
                                    class="inline-flex items-center gap-1 rounded-md border border-sky-600/60 bg-sky-700/50 px-1.5 py-0.5 text-[10px] font-semibold text-sky-100 hover:bg-sky-600/60"
                                    title="Open in target provider">
                                    <ExternalLink
                                        class="inline h-3.5 w-3.5 text-[11px]" />
                                    {titleCase(item.target_namespace || "target")}
                                </a>
                                <!-- eslint-enable svelte/no-navigation-without-resolve -->
                            {/if}

                            {#if item.source_media?.external_url}
                                <!-- eslint-disable svelte/no-navigation-without-resolve -->
                                <a
                                    href={item.source_media.external_url}
                                    target="_blank"
                                    rel="noopener"
                                    class="inline-flex items-center gap-1 rounded-md border border-amber-600 bg-amber-700/60 px-1.5 py-0.5 text-[10px] text-amber-100 transition-colors hover:bg-amber-600/60"
                                    title="Open in source provider">
                                    <ExternalLink
                                        class="inline h-3.5 w-3.5 text-[11px]" />
                                    {titleCase(item.source_namespace || "source")}
                                </a>
                                <!-- eslint-enable svelte/no-navigation-without-resolve -->
                            {/if}

                            {#if mappingUrl(item)}
                                <!-- eslint-disable svelte/no-navigation-without-resolve -->
                                <a
                                    href={mappingUrl(item)!}
                                    class="inline-flex items-center gap-1 rounded-md border border-slate-300/40 bg-white/10 px-1.5 py-0.5 text-[10px] font-semibold text-white hover:bg-white/20"
                                    title="View mapping">
                                    <Hash
                                        class="inline h-3.5 w-3.5 text-[11px] text-white" />
                                    Mapping
                                </a>
                                <!-- eslint-enable svelte/no-navigation-without-resolve -->
                            {/if}

                            {#if timestampLabel}
                                <span
                                    class="inline-flex items-center rounded bg-slate-800/70 px-1.5 py-0.5 tracking-wide text-slate-200 uppercase">
                                    {timestampLabel}
                                </span>
                            {/if}

                            {#if item.target_media?.labels}
                                {#each item.target_media?.labels as label (label)}
                                    <span
                                        class="inline-flex items-center rounded bg-slate-800/70 px-1.5 py-0.5 tracking-wide text-slate-200 uppercase">
                                        {label}
                                    </span>
                                {/each}
                            {/if}
                        </div>
                        <div class="mt-2 flex items-center gap-2">
                            {#if canShowDiff(item)}
                                <button
                                    type="button"
                                    onclick={() => toggleDiff(item.id)}
                                    class={`diff-toggle inline-flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 ${openDiff ? "open" : ""}`}
                                    aria-expanded={openDiff}>
                                    <span
                                        class="relative inline-flex h-4 w-4 items-center justify-center overflow-hidden">
                                        {#if openDiff}
                                            <span
                                                in:fade={{ duration: 140 }}
                                                out:fade={{ duration: 90 }}
                                                class="absolute inset-0 flex items-center justify-center">
                                                <SquareMinus
                                                    class="diff-icon inline h-4 w-4 text-[14px]" />
                                            </span>
                                        {:else}
                                            <span
                                                in:fade={{ duration: 140 }}
                                                out:fade={{ duration: 90 }}
                                                class="absolute inset-0 flex items-center justify-center">
                                                <SquarePlus
                                                    class="diff-icon inline h-4 w-4 text-[14px]" />
                                            </span>
                                        {/if}
                                    </span>
                                    <span class="diff-label relative"
                                        >{openDiff ? "Hide diff" : "Show diff"}</span>
                                    {#if diffCount !== undefined}
                                        <span
                                            class="rounded bg-slate-800/70 px-1 text-[10px] font-semibold text-slate-200">
                                            {diffCount}
                                        </span>
                                    {/if}
                                </button>
                            {/if}
                            {#if hasPins && togglePins}
                                <button
                                    type="button"
                                    class={`diff-toggle inline-flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 ${
                                        openPins ? "open" : ""
                                    }`}
                                    onclick={() => togglePins(item)}
                                    aria-expanded={openPins}
                                    aria-pressed={openPins}
                                    aria-busy={pinButtonLoading}
                                    disabled={pinButtonLoading || pinButtonDisabled}>
                                    <span
                                        class="relative inline-flex h-4 w-4 items-center justify-center overflow-hidden">
                                        {#if openPins}
                                            <span
                                                in:fade={{ duration: 140 }}
                                                out:fade={{ duration: 90 }}
                                                class="absolute inset-0 flex items-center justify-center">
                                                <SquareMinus
                                                    class="diff-icon inline h-4 w-4 text-[14px]" />
                                            </span>
                                        {:else}
                                            <span
                                                in:fade={{ duration: 140 }}
                                                out:fade={{ duration: 90 }}
                                                class="absolute inset-0 flex items-center justify-center">
                                                <SquarePlus
                                                    class="diff-icon inline h-4 w-4 text-[14px]" />
                                            </span>
                                        {/if}
                                        {#if pinButtonLoading}
                                            <span
                                                class="absolute inset-0 flex items-center justify-center rounded-full bg-slate-950/70"
                                                in:fade={{ duration: 100 }}
                                                out:fade={{ duration: 100 }}>
                                                <LoaderCircle
                                                    class="inline h-3.5 w-3.5 animate-spin text-sky-300" />
                                            </span>
                                        {/if}
                                    </span>
                                    <span class="diff-label relative"
                                        >{openPins ? "Hide pins" : "Show pins"}</span>
                                    {#if pinCount !== undefined}
                                        <span
                                            class="rounded bg-slate-800/70 px-1 text-[10px] font-semibold text-slate-200">
                                            {pinCount}
                                        </span>
                                    {/if}
                                </button>
                            {/if}
                            {#if hasInfo}
                                <button
                                    type="button"
                                    class={`diff-toggle inline-flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 ${
                                        openInfo ? "open" : ""
                                    }`}
                                    onclick={() => (openInfo = !openInfo)}
                                    aria-expanded={openInfo}
                                    title="Show sync details">
                                    <Info
                                        class="diff-icon inline h-4 w-4 text-[14px]" />
                                    <span class="diff-label relative"
                                        >{openInfo ? "Hide debug" : "Debug"}</span>
                                    <span
                                        class="rounded bg-slate-800/70 px-1 text-[10px] font-semibold text-slate-200">
                                        {infoEntries.length}
                                    </span>
                                </button>
                            {/if}
                            {#if item.error_message}
                                <div class="text-[11px] whitespace-nowrap text-red-400">
                                    {item.error_message}
                                </div>
                            {/if}
                        </div>
                    </div>
                </div>
                <div
                    class="flex shrink-0 flex-col items-stretch gap-1 self-start sm:gap-2">
                    {#if deleteHistory}
                        <button
                            type="button"
                            onclick={() => deleteHistory?.(item)}
                            class="inline-flex h-8 w-8 items-center justify-center gap-1 rounded-md border border-red-600/80 bg-red-700/70 px-2 text-[10px] font-medium text-red-100 hover:bg-red-600/70"
                            title="Delete history entry">
                            <Trash2 class="inline h-4 w-4" />
                        </button>
                    {/if}
                </div>
            </header>
        </div>
    </div>
</div>
{#if openDiff && canShowDiff(item)}
    <TimelineDiffViewer
        {item}
        ui={ui()} />
{/if}
{#if openPins && hasPins}
    {@render (pinsPanel ?? DefaultPins)({
        item,
        profile,
        openPins,
        pinCount,
        pinButtonLoading,
        pinButtonDisabled,
        togglePins,
        data: pinsPanelData,
    })}
{/if}
{#if openInfo && hasInfo}
    <div
        class="mt-2 ml-6 overflow-hidden rounded-md border border-slate-800 bg-slate-950/80 will-change-transform">
        <div
            class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-3 py-2">
            <div class="flex items-start gap-2 text-[10px]">
                <Info class="mt-0.5 h-3.5 w-3.5 text-slate-300" />
                <div class="flex items-center gap-2">
                    <span class="font-semibold tracking-wide text-slate-100 uppercase">
                        Sync Debug
                    </span>
                    <span
                        class="text-[11px] leading-tight font-normal text-slate-500 normal-case">
                        Debug info captured during sync.
                    </span>
                </div>
            </div>
            <div class="flex items-center gap-2 text-[11px] text-slate-400">
                <span
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1">
                    <Info class="h-3.5 w-3.5" />
                    <span class="font-semibold text-slate-100">
                        {infoEntries.length}
                    </span>
                    fields
                </span>
            </div>
        </div>
        <div class="overflow-auto p-3">
            <dl class="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
                {#each infoEntries as [key, value] (key)}
                    <div
                        class="rounded-md border border-slate-800/80 bg-slate-900/70 px-3 py-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
                        <dt
                            class="mb-1 inline-flex max-w-full rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] font-semibold tracking-[0.08em] text-slate-300 uppercase"
                            title={formatInfoKey(key)}>
                            <span class="truncate">{formatInfoKey(key)}</span>
                        </dt>
                        <dd
                            class="font-mono text-[11px] leading-5 wrap-break-word text-slate-100">
                            {value}
                        </dd>
                    </div>
                {/each}
            </dl>
        </div>
    </div>
{/if}

<style>
    .timeline-cover {
        aspect-ratio: 3 / 4;
        height: 78px;
        width: 54px;
    }
    .chip-scroll {
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
    }
    .chip-scroll::-webkit-scrollbar {
        display: none;
    }
    .diff-toggle {
        position: relative;
        transition: color 0.25s ease;
    }
    .diff-toggle.open {
        animation: diffPulse 900ms ease;
    }
    @keyframes diffPulse {
        0% {
            text-shadow: 0 0 0 rgba(56, 189, 248, 0);
        }
        35% {
            text-shadow: 0 0 6px rgba(56, 189, 248, 0.7);
        }
        100% {
            text-shadow: 0 0 0 rgba(56, 189, 248, 0);
        }
    }
    .diff-toggle:disabled {
        cursor: not-allowed;
        opacity: 0.6;
    }
    @media (prefers-reduced-motion: reduce) {
        .diff-toggle.open {
            animation: none;
        }
    }
</style>
