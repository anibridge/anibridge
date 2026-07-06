<script lang="ts">
    import { MoreHorizontal, RotateCcw, Trash2, Undo2 } from "@lucide/svelte";
    import { Popover } from "bits-ui";

    import type { HistoryGroup, HistoryOperation } from "$lib/types/api";

    interface Props {
        group: HistoryGroup;
        operation?: HistoryOperation | null;
        disabled?: boolean;
        onRetry?: (group: HistoryGroup) => void;
        onDeleteGroup?: (group: HistoryGroup) => void;
        onUndoOperation?: (operation: HistoryOperation) => void;
        onDeleteOperation?: (operation: HistoryOperation) => void;
    }

    let {
        group,
        operation = null,
        disabled = false,
        onRetry,
        onDeleteGroup,
        onUndoOperation,
        onDeleteOperation,
    }: Props = $props();

    let open = $state(false);

    const canRetry = $derived(["failed", "not_found"].includes(group.outcome));
    const canUndo = $derived(
        !!operation &&
            operation.resource_kind === "record" &&
            (!!operation.before_state || !!operation.after_state),
    );
    const hasActions = $derived(
        operation ? canUndo || !!onDeleteOperation : canRetry || !!onDeleteGroup,
    );

    function run(action: () => void) {
        open = false;
        action();
    }
</script>

{#if hasActions}
    <Popover.Root bind:open>
        <Popover.Trigger
            class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-700/70 bg-slate-900/70 text-slate-400 shadow-sm transition-colors hover:border-slate-600 hover:bg-slate-800 hover:text-slate-100 focus:ring-2 focus:ring-slate-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            aria-label="Timeline actions"
            title="Timeline actions"
            {disabled}>
            <MoreHorizontal class="h-4 w-4" />
        </Popover.Trigger>
        <Popover.Content
            class="z-50 w-52 rounded-md border border-slate-700 bg-slate-900/95 p-1.5 text-xs shadow-xl ring-1 ring-slate-800/70 backdrop-blur-sm"
            side="bottom"
            align="end"
            sideOffset={6}>
            <div class="space-y-1">
                {#if operation}
                    {#if canUndo && onUndoOperation}
                        <button
                            type="button"
                            class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sky-200 hover:bg-sky-600/20"
                            onclick={() => run(() => onUndoOperation?.(operation))}>
                            <Undo2 class="h-3.5 w-3.5" />
                            <span>Undo operation</span>
                        </button>
                    {/if}
                    {#if onDeleteOperation}
                        <button
                            type="button"
                            class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-rose-200 hover:bg-rose-600/20"
                            onclick={() => run(() => onDeleteOperation?.(operation))}>
                            <Trash2 class="h-3.5 w-3.5" />
                            <span>Delete operation</span>
                        </button>
                    {/if}
                {:else}
                    {#if canRetry && onRetry}
                        <button
                            type="button"
                            class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-emerald-200 hover:bg-emerald-600/20"
                            onclick={() => run(() => onRetry?.(group))}>
                            <RotateCcw class="h-3.5 w-3.5" />
                            <span>Retry group</span>
                        </button>
                    {/if}
                    {#if onDeleteGroup}
                        <button
                            type="button"
                            class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-rose-200 hover:bg-rose-600/20"
                            onclick={() => run(() => onDeleteGroup?.(group))}>
                            <Trash2 class="h-3.5 w-3.5" />
                            <span>Delete group</span>
                        </button>
                    {/if}
                {/if}
            </div>
        </Popover.Content>
    </Popover.Root>
{/if}
