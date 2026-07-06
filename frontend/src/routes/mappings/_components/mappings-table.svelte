<script lang="ts">
    import { Tooltip } from "bits-ui";

    import type { Mapping, MappingEdge } from "$lib/types/api";
    import { preferredTitle } from "$lib/utils/anilist";
    import { externalAuthorityUrl } from "$lib/utils/authority-links";
    import type { ColumnConfig } from "./columns";
    import MappingCard from "./mapping-card.svelte";

    export interface Props {
        items: Mapping[];
        columns?: ColumnConfig[];
        onEdit?: (payload: { mapping: Mapping }) => void;
        onNavigateToQuery?: (payload: { query: string }) => void;
    }

    let {
        items = $bindable([]),
        columns = $bindable([]),
        onEdit,
        onNavigateToQuery,
    }: Props = $props();

    if (!columns.length) {
        columns = [];
    }

    let resizing = $state<{
        columnId: string;
        startX: number;
        startWidth: number;
    } | null>(null);

    function startResize(e: MouseEvent, columnId: string) {
        if (!resizing) {
            const column = columns.find((c) => c.id === columnId);
            if (column && column.resizable) {
                resizing = { columnId, startX: e.clientX, startWidth: column.width };
                e.preventDefault();
            }
        }
    }

    function onMouseMove(e: MouseEvent) {
        if (resizing) {
            const diff = e.clientX - resizing.startX;
            const newWidth = Math.max(
                columns.find((c) => c.id === resizing!.columnId)?.minWidth || 60,
                resizing.startWidth + diff,
            );

            columns = columns.map((col) =>
                col.id === resizing!.columnId ? { ...col, width: newWidth } : col,
            );
        }
    }

    function onMouseUp() {
        if (resizing) {
            resizing = null;
        }
    }

    const visibleColumns = $derived(columns.filter((c) => c.visible));

    function navigate(query: string) {
        const text = query.trim();
        if (!text) return;
        onNavigateToQuery?.({ query: text });
    }

    function authorityFromColumn(columnId: string): string | null {
        return columnId.startsWith("authority:") ? columnId.slice(9) : null;
    }

    function formatDestinationRange(value: string | null | undefined): string {
        if (value === null || value === undefined) return "null";
        return value === "" ? '""' : value;
    }

    function edgeKey(edge: MappingEdge) {
        return `${edge.target_authority}:${edge.target_value}:${edge.target_scope ?? ""}:${edge.source_range}:${formatDestinationRange(edge.destination_range)}`;
    }

    function targetMappingQuery(edge: MappingEdge): string {
        const terms = [
            `target.authority:${edge.target_authority}`,
            `target.value:${edge.target_value}`,
        ];
        if (edge.target_scope) terms.push(`target.scope:${edge.target_scope}`);
        return terms.join(" ");
    }
</script>

<svelte:window
    onmousemove={onMouseMove}
    onmouseup={onMouseUp} />

<div class="flex-1 overflow-auto">
    <div class="relative min-w-0">
        <table
            class="w-full align-middle text-xs"
            style="table-layout: fixed;">
            <thead
                class="sticky top-0 z-10 bg-(--color-surface-alt)/60 text-[11px] tracking-wide text-(--color-dimmed) uppercase">
                <tr class="divide-x divide-slate-800/70 whitespace-nowrap">
                    {#each visibleColumns as column, i (column.id)}
                        <th
                            class="relative overflow-hidden px-3 py-2 text-left font-medium whitespace-nowrap"
                            style="width: {column.width}px;">
                            <div class="flex items-center justify-between">
                                <span
                                    class="truncate uppercase"
                                    title={column.title}>{column.title}</span>
                            </div>

                            {#if column.resizable && i < visibleColumns.length - 1}
                                <div
                                    class="absolute top-0 right-0 h-full w-1 cursor-col-resize opacity-0 transition-opacity hover:bg-slate-500 hover:opacity-100"
                                    onmousedown={(e) => startResize(e, column.id)}
                                    role="button"
                                    tabindex="-1"
                                    aria-label="Resize column">
                                </div>
                            {/if}
                        </th>
                    {/each}
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60">
                {#each items as m (m.descriptor)}
                    <tr class="align-top transition-colors hover:bg-slate-800/40">
                        {#each visibleColumns as column (column.id)}
                            <td
                                class="min-w-0 overflow-hidden px-3 py-2 whitespace-nowrap"
                                style="width: {column.width}px;">
                                {#if column.id === "title"}
                                    <div class="flex min-w-0 items-center gap-2">
                                        {#if m.anilist}
                                            {@const coverImage =
                                                m.anilist?.coverImage?.medium ?? null}
                                            {#if coverImage}
                                                <div
                                                    class="relative h-16 w-12 overflow-hidden rounded-md ring-1 ring-slate-700/60">
                                                    <img
                                                        alt={(preferredTitle(
                                                            m.anilist?.title,
                                                        ) || "Cover") + " cover"}
                                                        loading="lazy"
                                                        src={coverImage}
                                                        class="h-full w-full object-cover"
                                                        class:blur-sm={m.anilist
                                                            ?.isAdult} />
                                                </div>
                                            {:else}
                                                <div
                                                    class="flex h-16 w-12 shrink-0 items-center justify-center rounded-md border border-dashed border-slate-700 bg-slate-800/30 text-[9px] text-slate-500 select-none">
                                                    No Art
                                                </div>
                                            {/if}
                                        {:else}
                                            <div
                                                class="flex h-16 w-12 shrink-0 items-center justify-center rounded-md border border-dashed border-slate-700 bg-slate-800/30 text-[9px] text-slate-500 select-none">
                                                No Art
                                            </div>
                                        {/if}
                                        <div class="min-w-0 flex-1 space-y-1">
                                            <div
                                                class="truncate font-medium text-slate-100"
                                                title={preferredTitle(
                                                    m.anilist?.title,
                                                ) || m.descriptor}>
                                                {#if m?.anilist?.title}{preferredTitle(
                                                        m.anilist.title,
                                                    )}{:else}{m.descriptor}{/if}
                                            </div>
                                            <div
                                                class="flex flex-wrap gap-1 overflow-hidden text-[9px] text-slate-400">
                                                {#if m.anilist}
                                                    {#if m.anilist.format}<span
                                                            class="truncate rounded bg-slate-800/70 px-1 py-0.5 tracking-wide uppercase"
                                                            title={m.anilist.format}
                                                            >{m.anilist.format.replaceAll(
                                                                "_",
                                                                " ",
                                                            )}</span>
                                                    {/if}
                                                    {#if m.anilist.status}<span
                                                            class="truncate rounded bg-slate-800/70 px-1 py-0.5 tracking-wide uppercase"
                                                            title={m.anilist.status}
                                                            >{m.anilist.status.replaceAll(
                                                                "_",
                                                                " ",
                                                            )}</span>
                                                    {/if}
                                                    {#if m.anilist.season && m.anilist.seasonYear}<span
                                                            class="truncate rounded bg-slate-800/70 px-1 py-0.5 tracking-wide uppercase"
                                                            title={`${m.anilist.season} ${m.anilist.seasonYear}`}
                                                            >{m.anilist.season}
                                                            {m.anilist
                                                                .seasonYear}</span>
                                                    {/if}
                                                    {#if m.anilist.episodes}<span
                                                            class="truncate rounded bg-slate-800/70 px-1 py-0.5"
                                                            title={`${m.anilist.episodes} episodes`}
                                                            >EP {m.anilist
                                                                .episodes}</span>
                                                    {/if}
                                                    {#if m.anilist?.isAdult}
                                                        <span
                                                            class="rounded bg-rose-800 px-1 py-0.5"
                                                            title="Adult content"
                                                            >ADULT</span>
                                                    {/if}
                                                {/if}
                                                {#if m.custom}
                                                    <span
                                                        class="rounded bg-amber-600/30 px-1 py-0.5 text-amber-100 uppercase ring-1 ring-amber-700/50"
                                                        >Custom</span>
                                                {/if}
                                            </div>
                                        </div>
                                    </div>
                                {:else if authorityFromColumn(column.id)}
                                    {@const authority = authorityFromColumn(column.id)!}
                                    {@const edges = m.edges.filter(
                                        (e) => e.target_authority === authority,
                                    )}
                                    {#if m.authority === authority}
                                        <MappingCard
                                            tone="source"
                                            value={m.value}
                                            scope={m.scope}
                                            label="Source"
                                            url={externalAuthorityUrl(
                                                authority,
                                                m.value,
                                                m.scope,
                                            )}
                                            onNavigate={() =>
                                                navigate(
                                                    `source.authority:${authority} source.value:${m.value}`,
                                                )} />
                                    {:else if edges.length}
                                        <div
                                            class="flex flex-nowrap items-center gap-2 overflow-x-auto"
                                            style="white-space: nowrap;"
                                            title={`Targets mapped from ${authority}`}>
                                            {#each edges as edge (edgeKey(edge))}
                                                <MappingCard
                                                    value={edge.target_value}
                                                    scope={edge.target_scope}
                                                    url={externalAuthorityUrl(
                                                        edge.target_authority,
                                                        edge.target_value,
                                                        edge.target_scope,
                                                    )}
                                                    meta={[
                                                        `${edge.source_range} → ${formatDestinationRange(edge.destination_range)}`,
                                                    ]}
                                                    onNavigate={() =>
                                                        navigate(
                                                            targetMappingQuery(edge),
                                                        )} />
                                            {/each}
                                        </div>
                                    {:else}
                                        <span class="text-[10px] text-slate-500"
                                            >-</span>
                                    {/if}
                                {:else if column.id === "sources"}
                                    {#key (m.sources ?? []).join("|") + ":" + String(m.custom)}
                                        {@const total = (m.sources ?? []).length}
                                        <div>
                                            {#if total > 0}
                                                <Tooltip.Root>
                                                    <Tooltip.Trigger>
                                                        <span
                                                            class={`inline-flex h-5 min-w-5 items-center justify-center rounded px-1.5 text-[10px] ring-1 ${total > 1 ? "bg-amber-600/30 text-amber-100 ring-amber-700/40" : "bg-slate-800/60 text-slate-300 ring-slate-700/50"}`}
                                                            >{total}</span>
                                                    </Tooltip.Trigger>
                                                    <Tooltip.Portal>
                                                        <Tooltip.Content
                                                            collisionPadding={12}
                                                            side="bottom"
                                                            sideOffset={6}
                                                            class="max-h-27 max-w-[90vw] overflow-auto rounded-md border border-slate-700 bg-slate-900 p-2 text-left text-[11px] shadow-lg">
                                                            <ol class="space-y-1">
                                                                {#each m.sources ?? [] as s, i (i)}
                                                                    <li
                                                                        class="flex items-start gap-1 wrap-break-word">
                                                                        <span
                                                                            class="text-slate-500"
                                                                            >{i +
                                                                                1}.</span>
                                                                        <span
                                                                            class="text-slate-300"
                                                                            title={s}
                                                                            >{s}</span>
                                                                    </li>
                                                                {/each}
                                                            </ol>
                                                        </Tooltip.Content>
                                                    </Tooltip.Portal>
                                                </Tooltip.Root>
                                            {:else}
                                                <span class="text-[10px] text-slate-500"
                                                    >-</span>
                                            {/if}
                                        </div>
                                    {/key}
                                {:else if column.id === "actions"}
                                    <div
                                        class="flex justify-end gap-1 whitespace-nowrap">
                                        <button
                                            class="inline-flex h-6 items-center rounded-md bg-emerald-700 px-2 text-[11px] text-emerald-50 hover:bg-emerald-600"
                                            onclick={() => onEdit?.({ mapping: m })}
                                            >Edit</button>
                                    </div>
                                {/if}
                            </td>
                        {/each}
                    </tr>
                {/each}
                {#if !items.length}
                    <tr>
                        <td
                            colspan={visibleColumns.length}
                            class="py-8 text-center text-slate-500">
                            No mappings found
                        </td>
                    </tr>
                {/if}
            </tbody>
        </table>
    </div>
</div>

<style>
    .scroll-wrapper {
        position: relative;
    }

    .scroll-wrapper:hover::after,
    .scroll-wrapper:focus-within::after {
        opacity: 0.85;
    }

    .scroll-row {
        display: flex;
        align-items: center;
        gap: 0.25rem;
        overflow-x: auto;
        white-space: nowrap;
        padding-bottom: 0.35rem;
        padding-right: 0.75rem;
        scrollbar-width: thin;
        scrollbar-gutter: stable both-edges;
    }

    .scroll-row::-webkit-scrollbar {
        height: 6px;
    }

    .scroll-row::-webkit-scrollbar-track {
        background: rgba(71, 85, 105, 0.35);
        border-radius: 9999px;
    }

    .scroll-row::-webkit-scrollbar-thumb {
        background: rgba(16, 185, 129, 0.45);
        border-radius: 9999px;
    }

    .scroll-row:hover::-webkit-scrollbar-thumb {
        background: rgba(16, 185, 129, 0.8);
    }
</style>
