<script lang="ts">
    import { LoaderCircle, Pin, PinOff, RefreshCcw } from "@lucide/svelte";
    import { SvelteSet } from "svelte/reactivity";

    import type { PinFieldOption } from "$lib/types/api";

    interface Props {
        value?: string[];
        baseline?: string[];
        options?: PinFieldOption[];
        loading?: boolean;
        saving?: boolean;
        disabled?: boolean;
        error?: string | null;
        optionsError?: string | null;
        missingMessage?: string | null;
        title?: string;
        subtitle?: string;
        showRefresh?: boolean;
        onChange?: (value: string[]) => void;
        onSave?: (value: string[]) => void;
        onRefresh?: (force: boolean) => void;
    }

    let {
        value = $bindable<string[]>([]),
        baseline = [],
        options = [],
        loading = false,
        saving = false,
        disabled = false,
        error = null,
        optionsError = null,
        missingMessage = null,
        title = "Pin fields",
        subtitle = "Choose the fields to keep unchanged when syncing.",
        showRefresh = true,
        onChange,
        onSave,
        onRefresh,
    }: Props = $props();

    let selected = $state<string[]>([...(Array.isArray(value) ? value : [])]);
    let internalBaseline = $state<string[]>([]);

    const selectedCount = $derived(selected.length);
    const hasChanges = $derived(!arraysEqual(selected, internalBaseline));
    const allSelected = $derived(
        options.length > 0 &&
            arraysEqual(
                selected,
                options.map((option) => option.value),
            ),
    );
    const showNoOptions = $derived(!loading && !options.length && !optionsError);

    function arraysEqual(a: string[], b: string[]): boolean {
        if (a.length !== b.length) return false;
        for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
        return true;
    }

    function updateValue(fields: string[]) {
        const next = [...fields];
        selected = next;
        value = [...next];
        onChange?.([...next]);
    }

    function toggleField(value: string) {
        const current = new SvelteSet(selected);
        if (current.has(value)) current.delete(value);
        else current.add(value);
        const ordered = options.length
            ? options.map((opt) => opt.value).filter((v) => current.has(v))
            : Array.from(current);
        updateValue(ordered);
    }

    function resetSelection() {
        if (!hasChanges) return;
        updateValue(internalBaseline);
    }

    function clearSelection() {
        if (!selected.length) return;
        updateValue([]);
    }

    function selectAll() {
        if (!options.length) return;
        if (allSelected) return;
        updateValue(options.map((option) => option.value));
    }

    function saveSelection() {
        if (saving || !hasChanges || disabled) return;
        onSave?.([...selected]);
    }

    function refreshOptions(force: boolean) {
        onRefresh?.(force);
    }

    $effect(() => {
        const next = Array.isArray(value) ? value : [];
        if (!arraysEqual(next, selected)) selected = [...next];
    });

    $effect(() => {
        const next = Array.isArray(baseline) ? baseline : [];
        if (!arraysEqual(next, internalBaseline)) internalBaseline = [...next];
    });
</script>

<div class="pin-editor mt-3 ml-6 text-[11px]">
    <div class="overflow-hidden rounded-md border border-slate-800 bg-slate-950/70">
        <div
            class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-3 py-2">
            <div class="flex items-start gap-2 text-[10px]">
                <Pin class="mt-0.5 h-3.5 w-3.5" />
                <div class="flex items-center gap-2">
                    <span class="font-semibold tracking-wide text-slate-100 uppercase">
                        {title}
                    </span>
                    <span
                        class="text-[11px] leading-tight font-normal text-slate-500 normal-case">
                        {subtitle}
                    </span>
                </div>
            </div>
            <div class="flex items-center gap-2 text-[11px] text-slate-400">
                <span
                    class={`inline-flex items-center gap-1 rounded-md border px-2 py-1 ${
                        hasChanges
                            ? "border-sky-500/60 bg-sky-500/10 text-sky-100"
                            : "border-slate-700 bg-slate-900/60"
                    }`}>
                    <Pin class="h-3.5 w-3.5" />
                    <span class="font-semibold text-slate-100">{selectedCount}</span>
                    selected
                </span>
                {#if showRefresh}
                    <button
                        type="button"
                        class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1 text-slate-200 hover:border-slate-600 disabled:opacity-60"
                        onclick={() => refreshOptions(true)}
                        disabled={loading}>
                        <RefreshCcw
                            class={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                        Refresh
                    </button>
                {/if}
            </div>
        </div>
        <div class="space-y-3 p-3">
            {#if error}
                <div
                    class="rounded-md border border-red-700/60 bg-red-900/20 px-3 py-2 text-red-100">
                    <span>{error}</span>
                </div>
            {/if}
            {#if missingMessage}
                <div
                    class="rounded-md border border-amber-600/60 bg-amber-900/20 px-3 py-2 text-amber-100">
                    {missingMessage}
                </div>
            {/if}
            {#if showNoOptions}
                <div
                    class="flex w-full items-center gap-2 rounded-md border border-dashed border-slate-800 px-3 py-2 text-slate-400">
                    <PinOff class="h-4 w-4" />
                    No pin fields available.
                </div>
            {:else}
                <div class="flex flex-wrap gap-2">
                    {#each options as option (option.value)}
                        {@const checked = selected.includes(option.value)}
                        <button
                            type="button"
                            class={`pin-option inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-medium transition ${
                                checked
                                    ? "border-sky-500/60 bg-sky-500/15 text-sky-100"
                                    : "border-slate-700 bg-slate-900/40 text-slate-300 hover:border-slate-600 hover:text-slate-100"
                            }`}
                            onclick={() => toggleField(option.value)}
                            aria-pressed={checked}
                            title={`${option.label} (${option.value})`}
                            disabled={disabled || loading}>
                            <span
                                class={`inline-block h-2.5 w-2.5 rounded-full ${
                                    checked
                                        ? "bg-sky-300 shadow-[0_0_0_1px_rgba(56,189,248,0.35)]"
                                        : "border border-slate-500 bg-slate-800"
                                }`}></span>
                            <span>{option.label}</span>
                        </button>
                    {/each}
                </div>
                {#if loading && options.length}
                    <div
                        class="flex w-full items-center gap-2 rounded-md border border-slate-800 bg-slate-900/40 px-3 py-2 text-slate-400">
                        <LoaderCircle class="inline h-3.5 w-3.5 animate-spin" />
                        Updating options…
                    </div>
                {/if}
            {/if}
            {#if optionsError}
                <div
                    class="flex flex-wrap items-center gap-2 rounded-md border border-amber-600/60 bg-amber-900/20 px-3 py-2 text-amber-100">
                    <span>{optionsError}</span>
                    {#if showRefresh}
                        <button
                            type="button"
                            class="ml-auto inline-flex items-center gap-1 rounded-md border border-amber-500/70 px-2.5 py-1 hover:border-amber-400"
                            onclick={() => refreshOptions(true)}>
                            <RefreshCcw class="h-3.5 w-3.5" />
                            Retry
                        </button>
                    {/if}
                </div>
            {/if}
            {#if loading && !options.length}
                <div class="flex items-center gap-2 text-slate-400">
                    <LoaderCircle class="inline h-3.5 w-3.5 animate-spin" />
                    Loading pin options…
                </div>
            {/if}
        </div>
        <div
            class="flex flex-wrap items-center gap-2 border-t border-slate-800 bg-slate-950/60 px-3 py-2">
            <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-sky-500/60 bg-sky-500/15 px-3 py-1.5 font-semibold text-sky-100 hover:bg-sky-500/25 disabled:opacity-60"
                onclick={saveSelection}
                disabled={!hasChanges || saving || loading || disabled}>
                {#if saving}
                    <LoaderCircle class="inline h-3.5 w-3.5 animate-spin" />
                {/if}
                Save pins
            </button>
            <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900/40 px-3 py-1.5 text-slate-200 hover:border-slate-500 disabled:opacity-60"
                onclick={resetSelection}
                disabled={!hasChanges || saving || loading || disabled}>
                Reset
            </button>
            <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900/40 px-3 py-1.5 text-slate-300 hover:border-slate-500 disabled:opacity-60"
                onclick={clearSelection}
                disabled={!selectedCount || saving || loading || disabled}>
                Clear
            </button>
            <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900/40 px-3 py-1.5 text-slate-200 hover:border-slate-500 disabled:opacity-60"
                onclick={selectAll}
                disabled={allSelected ||
                    saving ||
                    loading ||
                    !options.length ||
                    disabled}>
                Select all
            </button>
        </div>
    </div>
</div>
