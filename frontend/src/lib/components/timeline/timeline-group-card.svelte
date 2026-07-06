<script lang="ts">
    /* eslint-disable svelte/no-navigation-without-resolve */

    import { ArrowRight, ExternalLink, Map } from "@lucide/svelte";

    import { resolve } from "$app/paths";
    import TimelineActionMenu from "$lib/components/timeline/timeline-action-menu.svelte";
    import TimelineOperationList from "$lib/components/timeline/timeline-operation-list.svelte";
    import { outcomeMeta } from "$lib/components/timeline/types";
    import type {
        HistoryGroup,
        HistoryOperation,
        ProviderMediaMetadata,
    } from "$lib/types/api";
    import {
        qualifiedRefLabel,
        refLabel,
        targetIdentifier,
    } from "$lib/utils/provider-ref";

    interface Props {
        group: HistoryGroup;
        disabled?: boolean;
        onRetry?: (group: HistoryGroup) => void;
        onDeleteGroup?: (group: HistoryGroup) => void;
        onUndoOperation?: (operation: HistoryOperation) => void;
        onDeleteOperation?: (operation: HistoryOperation) => void;
    }

    let {
        group,
        disabled = false,
        onRetry,
        onDeleteGroup,
        onUndoOperation,
        onDeleteOperation,
    }: Props = $props();

    function formatDate(ts: string): string {
        return new Date(ts).toLocaleString();
    }

    function mediaTitle(
        media?: ProviderMediaMetadata | null,
        fallback?: string | null,
    ): string {
        return media?.title || fallback || "Unknown item";
    }

    function posterFallback(
        media?: ProviderMediaMetadata | null,
        fallback?: string | null,
    ): string {
        const value = mediaTitle(media, fallback).trim();
        return value.slice(0, 1).toUpperCase() || "?";
    }

    function mappingHref(
        side: "source" | "target",
        authority?: string | null,
        value?: string | null,
        scope?: string | null,
    ): string | null {
        if (!authority || !value) return null;
        const terms = [`${side}.authority:${authority}`, `${side}.value:${value}`];
        if (scope) terms.push(`${side}.scope:${scope}`);
        const query = terms.join(" ");
        return resolve(`/mappings?q=${encodeURIComponent(query)}`);
    }

    const meta = $derived(outcomeMeta(group.outcome));
    const OutcomeIcon = $derived(meta.icon);
    const sourceMappingHref = $derived(
        mappingHref(
            "source",
            group.animap_authority,
            group.animap_value,
            group.animap_scope,
        ),
    );
    const targetMappingHref = $derived(
        (() => {
            const target = targetIdentifier(group);
            return target ? mappingHref("target", target.namespace, target.key) : null;
        })(),
    );
    const sourceParentLabel = $derived(
        qualifiedRefLabel(group.source_namespace, group.source_parent_ref),
    );
    const targetParentLabel = $derived(
        qualifiedRefLabel(group.target_namespace, group.target_parent_ref),
    );
</script>

<article
    class="overflow-hidden rounded-md border border-slate-800/80 bg-slate-900/50 shadow-sm transition-colors hover:bg-slate-900/70">
    <div class="space-y-2.5 p-3">
        <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                    <span
                        class={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium ${meta.badgeClass}`}
                        title={`run #${group.run_id} · group #${group.id}`}>
                        <OutcomeIcon class="h-3.5 w-3.5" />
                        {meta.label}
                    </span>
                    <time
                        class="text-[11px] text-slate-500"
                        datetime={group.timestamp}
                        title={formatDate(group.timestamp)}>
                        {formatDate(group.timestamp)}
                    </time>
                    {#if group.ephemeral}
                        <span
                            class="rounded-md border border-indigo-700/50 bg-indigo-600/15 px-2 py-0.5 text-[11px] text-indigo-200">
                            dry run
                        </span>
                    {/if}
                </div>
            </div>
            <TimelineActionMenu
                {group}
                {disabled}
                {onRetry}
                {onDeleteGroup} />
        </div>

        <div class="grid gap-2 lg:grid-cols-[1fr_auto_1fr] lg:items-stretch">
            <section
                class="flex gap-2 rounded-md border border-emerald-900/40 bg-emerald-950/20 p-2">
                <div
                    class="relative flex h-16 w-12 shrink-0 items-center justify-center overflow-hidden rounded-md border border-emerald-900/50 bg-slate-950/70 shadow-inner sm:h-20 sm:w-14">
                    {#if group.source_media?.poster_url}
                        <img
                            src={group.source_media.poster_url}
                            alt=""
                            loading="lazy"
                            class="max-h-full max-w-full object-contain" />
                    {:else}
                        <div
                            class="flex h-full w-full items-center justify-center bg-emerald-950/50 text-xl font-semibold text-emerald-300/70">
                            {posterFallback(
                                group.source_media,
                                refLabel(group.source_parent_ref),
                            )}
                        </div>
                    {/if}
                </div>
                <div class="min-w-0 flex-1">
                    <div class="line-clamp-2 text-sm leading-snug font-medium text-slate-100">
                        {mediaTitle(
                            group.source_media,
                            refLabel(group.source_parent_ref),
                        )}
                        {#if sourceParentLabel}
                            <span class="font-mono text-[11px] text-slate-500">
                                ({sourceParentLabel})
                            </span>
                        {/if}
                    </div>
                    {#if group.source_media?.labels?.length}
                        <div class="mt-1.5 flex flex-wrap gap-1">
                            {#each group.source_media.labels.slice(0, 2) as label (label)}
                                <span
                                    class="rounded bg-emerald-900/50 px-1.5 py-0.5 text-[10px] text-emerald-100/80">
                                    {label}
                                </span>
                            {/each}
                        </div>
                    {/if}
                    <div class="mt-1.5 flex flex-wrap gap-x-2 gap-y-1">
                        {#if group.source_media?.external_url}
                            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
                            <a
                                class="inline-flex items-center gap-1 text-[11px] text-emerald-200 hover:text-emerald-100"
                                href={group.source_media.external_url}
                                target="_blank"
                                rel="noreferrer">
                                Open source <ExternalLink class="h-3 w-3" />
                            </a>
                        {/if}
                        {#if sourceMappingHref}
                            <a
                                class="inline-flex items-center gap-1 text-[11px] text-emerald-200/80 hover:text-emerald-100"
                                href={sourceMappingHref}>
                                Mapping <Map class="h-3 w-3" />
                            </a>
                        {/if}
                    </div>
                </div>
            </section>

            <div class="hidden items-center text-slate-600 lg:flex">
                <ArrowRight class="h-3.5 w-3.5" />
            </div>

            <section
                class="flex gap-2 rounded-md border border-emerald-900/40 bg-emerald-950/20 p-2">
                <div
                    class="relative flex h-16 w-12 shrink-0 items-center justify-center overflow-hidden rounded-md border border-slate-800 bg-slate-950/70 shadow-inner sm:h-20 sm:w-14">
                    {#if group.target_media?.poster_url}
                        <img
                            src={group.target_media.poster_url}
                            alt=""
                            loading="lazy"
                            class="max-h-full max-w-full object-contain" />
                    {:else}
                        <div
                            class="flex h-full w-full items-center justify-center bg-slate-900/70 text-xl font-semibold text-slate-500">
                            {posterFallback(
                                group.target_media,
                                refLabel(group.target_parent_ref),
                            )}
                        </div>
                    {/if}
                </div>
                <div class="min-w-0 flex-1">
                    <div class="line-clamp-2 text-sm leading-snug font-medium text-slate-100">
                        {mediaTitle(
                            group.target_media,
                            refLabel(group.target_parent_ref),
                        )}
                        {#if targetParentLabel}
                            <span class="font-mono text-[11px] text-slate-500">
                                ({targetParentLabel})
                            </span>
                        {/if}
                    </div>
                    {#if group.target_media?.labels?.length}
                        <div class="mt-1.5 flex flex-wrap gap-1">
                            {#each group.target_media.labels.slice(0, 2) as label (label)}
                                <span
                                    class="rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] text-slate-300">
                                    {label}
                                </span>
                            {/each}
                        </div>
                    {/if}
                    <div class="mt-1.5 flex flex-wrap gap-x-2 gap-y-1">
                        {#if group.target_media?.external_url}
                            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
                            <a
                                class="inline-flex items-center gap-1 text-[11px] text-sky-200 hover:text-sky-100"
                                href={group.target_media.external_url}
                                target="_blank"
                                rel="noreferrer">
                                Open target <ExternalLink class="h-3 w-3" />
                            </a>
                        {/if}
                        {#if targetMappingHref}
                            <a
                                class="inline-flex items-center gap-1 text-[11px] text-sky-200/80 hover:text-sky-100"
                                href={targetMappingHref}>
                                Mapping <Map class="h-3 w-3" />
                            </a>
                        {/if}
                    </div>
                </div>
            </section>
        </div>

        {#if group.info}
            <div class="flex flex-wrap gap-1">
                {#each Object.entries(group.info).slice(0, 4) as [key, value] (key)}
                    <span
                        class="rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-slate-700/60">
                        {key}: {value}
                    </span>
                {/each}
            </div>
        {/if}
    </div>

    <TimelineOperationList
        {group}
        {disabled}
        {onUndoOperation}
        {onDeleteOperation} />
</article>
