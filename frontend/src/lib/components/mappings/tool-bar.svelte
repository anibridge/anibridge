<script lang="ts">
    import { Check, FolderTree, Plus } from "@lucide/svelte";
    import { twMerge } from "tailwind-merge";

    import BooruSearch from "$lib/components/mappings/booru-search.svelte";

    interface Props {
        class?: string;
        query?: string;
        customOnly?: boolean;
        page?: number;
        loading?: boolean;
        onLoad: () => void;
        onCancel: () => void;
        onSubmit?: () => void;
        onCreate?: () => void;
        onIncludes?: () => void;
    }

    let {
        class: className = "",
        query = $bindable(""),
        customOnly = $bindable(false),
        page = $bindable(1),
        loading = false,
        onLoad,
        onCancel,
        onCreate,
        onIncludes,
        onSubmit,
    }: Props = $props();

    function resetPage() {
        if (page !== 1) page = 1;
    }

    function handleSubmit() {
        resetPage();
        onSubmit?.();
        onLoad();
    }

    function toggleCustom() {
        customOnly = !customOnly;
        resetPage();
        onLoad();
    }
</script>

<!-- Desktop -->
<div class={twMerge("hidden items-center gap-2 text-[11px] sm:flex", className)}>
    <div class="relative w-64 md:w-96">
        <BooruSearch
            bind:value={query}
            size="md"
            {loading}
            {onCancel}
            onSubmit={handleSubmit} />
    </div>
    <button
        onclick={toggleCustom}
        class={`inline-flex h-8 items-center gap-1 rounded-md px-3 text-[11px] font-medium ring-1 transition-colors ${customOnly ? "bg-amber-600/30 text-amber-100 ring-amber-500/10 hover:bg-amber-500/30" : "bg-slate-800 text-slate-300 ring-slate-700/60 hover:bg-slate-700"}`}>
        {#if customOnly}
            <Check class="inline h-3.5 w-3.5 text-[14px]" />
        {:else}
            <svg
                class="inline h-3.5 w-3.5"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                ><rect
                    x="3"
                    y="3"
                    width="18"
                    height="18"
                    rx="2"
                    ry="2"></rect
                ></svg>
        {/if}
        <span>Custom Only</span>
    </button>
    <button
        title="Manage Includes"
        aria-label="Manage Includes"
        onclick={() => onIncludes?.()}
        class="inline-flex h-8 items-center gap-1 rounded-md bg-slate-800 px-3 text-[11px] font-medium text-slate-300 ring-1 ring-slate-700/60 transition-colors hover:bg-slate-700">
        <FolderTree class="inline h-3.5 w-3.5 text-[14px]" />
        <span>Includes</span>
    </button>
    <div class="ml-auto flex items-center gap-2">
        <button
            title="New Override"
            aria-label="New Override"
            onclick={() => onCreate?.()}
            class="inline-flex h-8 items-center gap-1 rounded-md bg-emerald-600/90 px-3 text-[11px] font-medium text-emerald-50 hover:bg-emerald-500">
            <Plus class="inline h-3.5 w-3.5 text-[14px]" />
        </button>
    </div>
</div>

<!-- Mobile -->
<div
    class={twMerge(
        "flex flex-col gap-3 rounded-md border border-slate-800/70 bg-slate-900/60 p-3 text-[11px] sm:hidden",
        className,
    )}>
    <div class="relative">
        <BooruSearch
            bind:value={query}
            size="md"
            {loading}
            {onCancel}
            onSubmit={() => {
                page = 1;
                onSubmit?.();
                onLoad();
            }} />
    </div>
    <div class="flex flex-wrap items-center justify-between">
        <div class="flex flex-wrap items-center gap-2">
            <button
                title="Manage Includes"
                aria-label="Manage Includes"
                onclick={() => onIncludes?.()}
                class="inline-flex h-8 items-center gap-1 rounded-md bg-slate-800 px-3 text-[11px] font-medium text-slate-300 ring-1 ring-slate-700/60 transition-colors hover:bg-slate-700">
                <FolderTree class="inline h-3.5 w-3.5" />
                <span>Includes</span>
            </button>
            <button
                onclick={toggleCustom}
                class={`inline-flex h-8 items-center gap-1 rounded-md px-3 text-[11px] font-medium ring-1 ${customOnly ? "bg-emerald-600/90 text-white ring-emerald-500/40 hover:bg-emerald-500" : "bg-slate-800 text-slate-300 ring-slate-700/60 hover:bg-slate-700"}`}>
                {#if customOnly}
                    <Check class="inline h-3.5 w-3.5" />
                {:else}
                    <svg
                        class="inline h-3.5 w-3.5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        ><rect
                            x="3"
                            y="3"
                            width="18"
                            height="18"
                            rx="2"
                            ry="2"></rect
                        ></svg>
                {/if}
                <span>Custom Only</span>
            </button>
        </div>
        <button
            title="New Override"
            aria-label="New Override"
            onclick={() => onCreate?.()}
            class="inline-flex h-8 items-center gap-1 rounded-md bg-emerald-600/90 px-3 text-[11px] font-medium text-emerald-50 hover:bg-emerald-500">
            <Plus class="inline h-3.5 w-3.5 text-[14px]" />
        </button>
    </div>
</div>
