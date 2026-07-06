<script lang="ts">
    import { Check, ChevronDown } from "@lucide/svelte";
    import { Checkbox, Collapsible } from "bits-ui";

    import type {
        HistoryGroup,
        HistoryOperation,
        RecordSnapshotValue,
    } from "$lib/types/api";
    import { qualifiedRefLabel } from "$lib/utils/provider-ref";
    import TimelineActionMenu from "./timeline-action-menu.svelte";
    import { outcomeMeta, resourceMeta } from "./types";

    interface Props {
        group: HistoryGroup;
        disabled?: boolean;
        onUndoOperation?: (operation: HistoryOperation) => void;
        onDeleteOperation?: (operation: HistoryOperation) => void;
        onTogglePin?: (target: HistoryOperation) => void;
    }

    let {
        group,
        disabled = false,
        onUndoOperation,
        onDeleteOperation,
        onTogglePin,
    }: Props = $props();

    let open = $state(false);
    let hideUnchanged = $state(true);

    $effect(() => {
        if ((group.error_count ?? 0) > 0) open = true;
    });

    function operationLabel(operation: HistoryOperation): string {
        const action = operation.action.replaceAll("_", " ");
        const resource = resourceMeta(operation.resource_kind).label.toLowerCase();
        return `${action} ${resource}`;
    }

    function countLabel(label: string, value?: number): string | null {
        if (!value) return null;
        return `${value} ${label}${value === 1 ? "" : "s"}`;
    }

    const resourceCounts = $derived(
        [
            countLabel("record", group.record_count),
            countLabel("event", group.event_count),
            countLabel("node", group.node_count),
        ].filter(Boolean) as string[],
    );

    function changedFields(operation: HistoryOperation): string[] {
        return [
            ...Object.keys(operation.before_state?.values ?? {}),
            ...Object.keys(operation.after_state?.values ?? {}),
        ]
            .filter((field, index, fields) => fields.indexOf(field) === index)
            .sort();
    }

    function visibleFields(operation: HistoryOperation): string[] {
        const fields = changedFields(operation);
        if (!hideUnchanged) return fields;
        return fields.filter((field) => !isUnchanged(operation, field));
    }

    function formatField(field: string): string {
        return field.replaceAll("_", " ");
    }

    function formatValue(value?: RecordSnapshotValue): string {
        if (!value) return "-";
        if (value.state?.status) return value.state.status;
        if (value.state?.native) return value.state.native;
        if (value.progress) {
            const current = value.progress.current ?? "?";
            const total =
                value.progress.total && value.progress.total > 0
                    ? value.progress.total
                    : "?";
            const unit = value.progress.unit ? ` ${value.progress.unit}` : "";
            return `${current}/${total}${unit}`;
        }
        if (value.rating) return `${value.rating.value}/${value.rating.scale[2]}`;
        if (value.scalar !== undefined) return String(value.scalar);
        if (value.date_value) return value.date_value;
        if (value.datetime_value)
            return new Date(value.datetime_value).toLocaleString();
        return "-";
    }

    function isUnchanged(operation: HistoryOperation, field: string): boolean {
        return (
            formatValue(operation.before_state?.values?.[field]) ===
            formatValue(operation.after_state?.values?.[field])
        );
    }

    function valueTone(
        operation: HistoryOperation,
        field: string,
        side: "before" | "after",
    ): string {
        if (isUnchanged(operation, field)) return "text-slate-500";
        const value =
            side === "before"
                ? operation.before_state?.values?.[field]
                : operation.after_state?.values?.[field];
        if (!value) return "text-slate-600";
        return side === "before" ? "text-rose-300" : "text-emerald-300";
    }
</script>

{#if group.operations?.length}
    <Collapsible.Root
        bind:open
        class="border-t border-slate-800/70">
        <Collapsible.Trigger
            class="flex w-full items-center justify-between gap-2 bg-slate-950/20 px-3 py-1.5 text-left text-xs text-slate-400 transition-colors hover:bg-slate-900/70 hover:text-slate-200">
            <span class="flex min-w-0 flex-wrap items-center gap-2">
                <span class="font-medium">
                    {group.operations.length} operation{group.operations.length === 1
                        ? ""
                        : "s"}
                </span>
                {#each resourceCounts as count (count)}
                    <span
                        class="rounded border border-slate-800/80 bg-slate-950/50 px-1.5 py-0.5 text-[10px] text-slate-400">
                        {count}
                    </span>
                {/each}
            </span>
            <ChevronDown
                class={`h-4 w-4 transition-transform ${open ? "rotate-180" : ""}`} />
        </Collapsible.Trigger>
        <Collapsible.Content>
            <div class="divide-y divide-slate-800/70 border-t border-slate-800/70">
                {#each group.operations as operation (operation.id)}
                    {@const meta = outcomeMeta(operation.outcome)}
                    {@const resource = resourceMeta(operation.resource_kind)}
                    {@const ResourceIcon = resource.icon}
                    {@const OutcomeIcon = meta.icon}
                    {@const fields = changedFields(operation)}
                    {@const diffFields = visibleFields(operation)}
                    {@const sourceLabel =
                        qualifiedRefLabel(
                            operation.source_namespace,
                            operation.source_ref,
                        ) ?? "-"}
                    {@const targetLabel =
                        qualifiedRefLabel(
                            operation.target_namespace,
                            operation.target_ref,
                        ) ?? "-"}
                    <div class="space-y-2 px-3 py-2.5">
                        <div
                            class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                            <div class="min-w-0 space-y-1">
                                <div class="flex flex-wrap items-center gap-2">
                                    <span
                                        class="inline-flex items-center gap-1 rounded-md border border-slate-700/70 bg-slate-900/70 px-2 py-0.5 text-[11px] font-medium text-slate-200">
                                        <ResourceIcon class="h-3.5 w-3.5" />
                                        {operationLabel(operation)}
                                    </span>
                                    <span
                                        class={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium ${meta.badgeClass}`}>
                                        <OutcomeIcon class="h-3.5 w-3.5" />
                                        {meta.label}
                                    </span>
                                    <span
                                        class="inline-flex max-w-full items-center gap-1 rounded-md border border-slate-800/80 bg-slate-950/50 px-2 py-0.5 font-mono text-[10px] text-slate-400">
                                        <span
                                            class="truncate"
                                            title={sourceLabel}>{sourceLabel}</span>
                                        <span class="text-slate-600">→</span>
                                        <span
                                            class="truncate"
                                            title={targetLabel}>{targetLabel}</span>
                                    </span>
                                    {#if operation.pinned}
                                        <span
                                            class="rounded-md border border-sky-700/50 bg-sky-600/15 px-2 py-0.5 text-[11px] text-sky-200">
                                            pinned
                                        </span>
                                    {/if}
                                </div>
                            </div>
                            <TimelineActionMenu
                                {group}
                                {operation}
                                {disabled}
                                {onUndoOperation}
                                {onDeleteOperation}
                                onTogglePin={onTogglePin
                                    ? (target) =>
                                          onTogglePin(target as HistoryOperation)
                                    : undefined} />
                        </div>

                        {#if operation.error_message}
                            <div
                                class="rounded-md border border-rose-800/50 bg-rose-950/30 px-2 py-1.5 text-xs text-rose-100">
                                {operation.error_message}
                            </div>
                        {/if}

                        {#if fields.length}
                            <div
                                class="overflow-hidden rounded-md border border-slate-800/70 bg-slate-950/40 text-[11px]">
                                <div
                                    class="flex items-center justify-between gap-2 border-b border-slate-800/70 bg-slate-950/70 px-2.5 py-1.5">
                                    <div
                                        class="text-[10px] font-medium tracking-wide text-slate-500 uppercase">
                                        Changes
                                    </div>
                                    <label
                                        class="flex cursor-pointer items-center gap-2 text-[11px] text-slate-400">
                                        <Checkbox.Root
                                            bind:checked={hideUnchanged}
                                            class="flex h-4 w-4 items-center justify-center rounded border border-slate-600 bg-slate-800 data-[state=checked]:border-sky-600 data-[state=checked]:bg-sky-600">
                                            {#snippet children({ checked })}
                                                {#if checked}
                                                    <Check class="h-3 w-3 text-white" />
                                                {/if}
                                            {/snippet}
                                        </Checkbox.Root>
                                        <span class="select-none">Hide unchanged</span>
                                    </label>
                                </div>
                                <div
                                    class="grid grid-cols-[minmax(5rem,0.7fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-slate-800/70 bg-slate-950/50 px-2.5 py-1 font-medium tracking-wide text-slate-500 uppercase">
                                    <span>Field</span>
                                    <span>Before</span>
                                    <span>After</span>
                                </div>
                                {#each diffFields as field (field)}
                                    <div
                                        class="grid grid-cols-[minmax(5rem,0.7fr)_minmax(0,1fr)_minmax(0,1fr)] gap-2 border-b border-slate-800/50 px-2.5 py-1.5 last:border-b-0 hover:bg-slate-900/40">
                                        <div
                                            class={`truncate font-medium capitalize ${isUnchanged(operation, field) ? "text-slate-500" : "text-slate-300"}`}
                                            title={formatField(field)}>
                                            {formatField(field)}
                                        </div>
                                        <div class="min-w-0 font-mono">
                                            <span
                                                class={`block truncate rounded bg-slate-900/60 px-1.5 py-0.5 ${valueTone(operation, field, "before")}`}
                                                title={formatValue(
                                                    operation.before_state?.values?.[
                                                        field
                                                    ],
                                                )}>
                                                {formatValue(
                                                    operation.before_state?.values?.[
                                                        field
                                                    ],
                                                )}
                                            </span>
                                        </div>
                                        <div class="min-w-0 font-mono">
                                            <span
                                                class={`block truncate rounded border border-sky-900/30 bg-sky-950/20 px-1.5 py-0.5 ${valueTone(operation, field, "after")}`}
                                                title={formatValue(
                                                    operation.after_state?.values?.[
                                                        field
                                                    ],
                                                )}>
                                                {formatValue(
                                                    operation.after_state?.values?.[
                                                        field
                                                    ],
                                                )}
                                            </span>
                                        </div>
                                    </div>
                                {/each}
                                {#if hideUnchanged && diffFields.length === 0}
                                    <div class="px-3 py-2 text-center text-slate-500">
                                        No changed fields to show.
                                    </div>
                                {/if}
                            </div>
                        {/if}

                        {#if operation.info && Object.keys(operation.info).length}
                            <div class="flex flex-wrap gap-1">
                                {#each Object.entries(operation.info) as [key, value] (key)}
                                    <span
                                        class="rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-slate-700/60">
                                        {key}: {value}
                                    </span>
                                {/each}
                            </div>
                        {/if}
                    </div>
                {/each}
            </div>
        </Collapsible.Content>
    </Collapsible.Root>
{/if}
