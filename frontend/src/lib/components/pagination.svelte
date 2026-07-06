<script lang="ts">
    import { twMerge } from "tailwind-merge";

    interface Props {
        class?: string;
        page?: number;
        pages?: number;
        perPage?: number;
        perPageOptions?: number[];
        hideIfSinglePage?: boolean;
        disabled?: boolean;
        showPerPage?: boolean;
        onChange?: (p: {
            type: "page" | "perPage";
            page: number;
            perPage: number;
        }) => void;
    }

    let {
        class: className = "",
        page = $bindable(1),
        pages = $bindable(1),
        perPage = $bindable(25),
        perPageOptions = [10, 25, 50, 100],
        hideIfSinglePage = true,
        disabled = false,
        showPerPage = true,
        onChange,
    }: Props = $props();

    const showPagination = $derived(!hideIfSinglePage || pages > 1);

    function clampPage(p: number) {
        if (!Number.isFinite(p) || p < 1) return 1;
        if (pages && p > pages) return pages;
        return p;
    }

    function prev() {
        if (disabled) return;
        if (page > 1) {
            page = page - 1;
            onChange?.({ type: "page", page, perPage });
        }
    }
    function next() {
        if (disabled) return;
        if (page < pages) {
            page = page + 1;
            onChange?.({ type: "page", page, perPage });
        }
    }

    function onPageInput(e: Event) {
        const v = Number((e.target as HTMLInputElement).value);
        page = clampPage(v);
        onChange?.({ type: "page", page, perPage });
    }

    function onPerPageChange(e: Event) {
        perPage = Number((e.target as HTMLSelectElement).value) || perPage;
        page = 1;
        onChange?.({ type: "perPage", page, perPage });
    }
</script>

{#if showPagination}
    <div
        class={twMerge("flex flex-wrap items-center gap-2 text-xs ", className)}
        data-component="pagination">
        {#if pages > 1}
            <button
                onclick={prev}
                disabled={disabled || page === 1}
                class="rounded-md bg-slate-800 px-3 py-2 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
                >Prev</button>
            <div class="flex items-center gap-1 rounded-md bg-slate-800/60 px-2 py-1">
                Page
                <input
                    type="number"
                    min="1"
                    max={pages}
                    bind:value={page}
                    class="h-8 w-14 [appearance:textfield] rounded-md border border-slate-700 bg-slate-900 px-1 text-center text-xs font-semibold [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                    onchange={onPageInput} />
                / {pages}
            </div>
            <button
                onclick={next}
                disabled={disabled || page === pages}
                class="rounded-md bg-slate-800 px-3 py-2 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
                >Next</button>
        {/if}
        {#if showPerPage}
            <span class="ml-auto flex items-center gap-2">
                <label
                    for="perPageSelect"
                    class="text-[11px] text-slate-400">Per Page</label>
                <select
                    id="perPageSelect"
                    bind:value={perPage}
                    class="h-8 rounded-md border border-slate-700/70 bg-slate-950/70 px-2 text-[11px] shadow-sm focus:border-slate-600 focus:bg-slate-950"
                    onchange={onPerPageChange}
                    {disabled}>
                    {#each perPageOptions as opt (opt)}
                        <option value={opt}>{opt}</option>
                    {/each}
                </select>
            </span>
        {/if}
    </div>
{/if}

<style>
    [data-component="pagination"] select:disabled,
    [data-component="pagination"] button:disabled {
        opacity: 0.55;
    }
</style>
