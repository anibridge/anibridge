<script lang="ts">
    import { ExternalLink } from "@lucide/svelte";

    interface Props {
        value: string;
        scope?: string | null;
        url?: string | null;
        label?: string;
        tone?: "source" | "target";
        meta?: Array<string | null | undefined>;
        onNavigate?: () => void;
    }

    let {
        value,
        scope = null,
        url = null,
        label = "",
        tone = "target",
        meta = [],
        onNavigate,
    }: Props = $props();

    const toneClasses = $derived(
        tone === "source"
            ? "border-emerald-800/40 bg-emerald-900/20 text-emerald-50"
            : "border-slate-700/50 bg-slate-900/40 text-slate-50",
    );

    const chipClasses = $derived(
        tone === "source"
            ? "bg-emerald-700/60 text-emerald-50"
            : "bg-slate-800 text-slate-200",
    );

    const metaChips = $derived((meta || []).filter(Boolean) as string[]);
</script>

<div class={`inline-flex rounded-lg border p-2 shadow-inner ${toneClasses}`}>
    <div class="flex items-center gap-1 font-mono text-[11px]">
        <button
            class={`cursor-pointer rounded px-0.5 text-left truncate max-w-[16rem] ${tone === "source" ? "text-emerald-200" : "text-emerald-300"} select-text hover:underline focus:outline-none`}
            type="button"
            title={`Filter by value ${value}`}
            onclick={onNavigate}>{value}</button>
        {#if url}
            <button
                class={`${tone === "source" ? "text-emerald-200/70 hover:text-emerald-100" : "text-slate-500 hover:text-emerald-300"} cursor-pointer transition-colors`}
                aria-label={`Open external ${value}`}
                title={`Open external ${value}`}
                type="button"
                onclick={() =>
                    window.open(String(url), "_blank", "noopener,noreferrer")}>
                <ExternalLink class="h-3 w-3" />
            </button>
        {/if}
        {#if scope}
            <span class={`rounded px-1 py-0.5 text-[10px] uppercase ${chipClasses}`}
                >{scope}</span>
        {/if}
        {#if label}
            <span class={`rounded px-1 py-0.5 text-[10px] uppercase ${chipClasses}`}
                >{label}</span>
        {/if}
        {#if metaChips.length}
            {#each metaChips as mItem (mItem)}
                <span
                    class="rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-200 ring-1 ring-slate-700/60"
                    >{mItem}</span>
            {/each}
        {/if}
    </div>
</div>
