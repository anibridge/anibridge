<script lang="ts">
    import { Funnel, X } from "@lucide/svelte";

    import type { OutcomeMeta } from "$lib/components/timeline/types";

    interface Props {
        meta: Record<string, OutcomeMeta>;
        stats: Record<string, number>;
        active?: string | null;
        onToggle?: (key: string) => void;
        onClear?: () => void;
    }

    let { meta, stats, active = null, onToggle, onClear }: Props = $props();

    function toggle(key: string) {
        onToggle?.(key);
    }

    function clear() {
        onClear?.();
    }

    const activeMeta = () => (active ? (meta[active] ?? null) : null);
</script>

<div class="flex flex-wrap gap-3">
    {#each Object.entries(meta) as [k, value] (k)}
        <button
            type="button"
            onclick={() => toggle(k)}
            class={`group relative min-w-36 flex-1 basis-[min(100%,9rem)] cursor-pointer overflow-hidden rounded-md p-3 text-left transition select-none ${active === k ? "border-sky-500 bg-sky-950/40 ring-1 ring-sky-400/60" : "border border-slate-800 bg-linear-to-br from-slate-900/70 to-slate-800/30 hover:border-slate-700"}`}
            title={active === k
                ? "Click to remove filter"
                : "Filter by " + value.label}>
            <div class="text-[10px] font-medium tracking-wide text-slate-400 uppercase">
                {value.label}
            </div>
            <div class="mt-1 text-2xl font-semibold tabular-nums">
                {stats[k] || 0}
            </div>
            {#if active === k}
                <div class="absolute top-1 right-1">
                    <span
                        class="mr-1 inline-flex items-center gap-0.5 rounded bg-sky-600/70 px-1 py-0.5 text-[9px] font-semibold text-white"
                        ><Funnel class="inline h-3 w-3" /> Active</span>
                </div>
            {/if}
        </button>
    {/each}
</div>
{#if activeMeta()}
    <div class="flex items-center gap-2 text-[11px] text-slate-400">
        <div>
            Filtering by
            <span class="font-semibold text-slate-200">{activeMeta()?.label}</span>
        </div>
        <button
            onclick={clear}
            class="flex items-center gap-1 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-sky-300 hover:bg-slate-700"
            ><X class="inline h-3.5 w-3.5" /> Clear</button>
    </div>
{/if}
