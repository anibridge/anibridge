<script lang="ts">
    import { Lock, Plus, RefreshCcw, Save, Trash2 } from "@lucide/svelte";

    import type { MappingConfig, MappingConfigPayload } from "$lib/types/api";
    import Modal from "$lib/ui/modal.svelte";
    import { apiFetch } from "$lib/utils/api";

    interface Props {
        open: boolean;
        onSaved?: (detail: MappingConfig) => void;
    }

    type IncludeRow = { id: string; value: string };

    let { open = $bindable(false), onSaved }: Props = $props();

    let rows = $state<IncludeRow[]>([]);
    let mappingsUrl = $state("");
    let configPath = $state("");
    let configFormat = $state("");
    let loading = $state(false);
    let saving = $state(false);
    let error = $state<string | null>(null);

    function makeKey(): string {
        return `include-${Math.random().toString(36).slice(2)}`;
    }

    function emptyRow(): IncludeRow {
        return { id: makeKey(), value: "" };
    }

    function hydrate(config: MappingConfig) {
        rows = config.includes.map((value) => ({ id: makeKey(), value }));
        mappingsUrl = config.mappings_url;
        configPath = config.path;
        configFormat = config.format;
    }

    function resetState() {
        rows = [];
        mappingsUrl = "";
        configPath = "";
        configFormat = "";
        loading = false;
        saving = false;
        error = null;
    }

    function addRow() {
        rows = [...rows, emptyRow()];
    }

    function removeRow(id: string) {
        rows = rows.filter((row) => row.id !== id);
    }

    function updateRow(id: string, value: string) {
        rows = rows.map((row) => (row.id === id ? { ...row, value } : row));
    }

    function buildPayload(): MappingConfigPayload {
        return { includes: rows.map((row) => row.value.trim()).filter(Boolean) };
    }

    async function loadConfig() {
        loading = true;
        error = null;
        try {
            const res = await apiFetch("/api/mappings/config", undefined, {
                silent: true,
            });
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            hydrate(
                ((await res.json()) as MappingConfig) || {
                    mappings_url: "",
                    includes: [],
                    path: "",
                    format: "",
                },
            );
        } catch (err) {
            console.error("Failed to load mappings includes", err);
            error = "Failed to load includes";
            rows = [];
        } finally {
            loading = false;
        }
    }

    async function handleSave() {
        saving = true;
        error = null;
        try {
            const payload = buildPayload();
            const res = await apiFetch(
                "/api/mappings/config",
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                },
                { successMessage: "Mappings includes updated" },
            );
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data = (await res.json()) as MappingConfig;
            hydrate(data);
            onSaved?.(data);
            open = false;
        } catch (err) {
            console.error("Failed to save mappings includes", err);
            error = "Failed to save includes";
        } finally {
            saving = false;
        }
    }

    $effect(() => {
        if (!open) {
            resetState();
            return;
        }

        loadConfig();
    });
</script>

<Modal
    bind:open
    contentClass="fixed top-1/2 left-1/2 z-50 w-full max-w-6xl max-h-[90vh] -translate-x-1/2 -translate-y-1/2 rounded-xl border border-slate-800 bg-slate-950/90 shadow-2xl ring-1 ring-slate-800/60 backdrop-blur flex flex-col overflow-hidden"
    bodyClass="space-y-4 p-4 overflow-y-auto flex-1 min-w-0"
    headerWrapperClass="border-b border-slate-800/80 bg-slate-900/70 px-4 py-3 flex-shrink-0"
    footerClass="flex items-center justify-end gap-3 border-t border-slate-800/70 bg-slate-900/70 px-4 py-3 flex-shrink-0"
    closeButtonClass="rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-slate-800/70 hover:text-slate-100">
    {#snippet titleChildren()}
        <div class="text-sm font-semibold tracking-wide text-slate-100">
            Mappings Includes
        </div>
    {/snippet}

    <div
        class="flex flex-wrap items-center justify-between gap-3 text-[11px] text-slate-400">
        <div class="space-y-1">
            {#if configPath}
                <div>
                    Editing <span class="font-mono text-slate-300">{configPath}</span>
                    {#if configFormat}
                        <span class="text-slate-500">({configFormat})</span>
                    {/if}
                </div>
            {/if}
            <p>
                These are the additional mapping sources included with the main mappings
                file.
            </p>
            <p>The first source is the configured mappings URL and cannot be edited.</p>
        </div>
        <div class="flex flex-wrap items-center gap-2 text-[11px]">
            <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-slate-700/60 bg-slate-800/60 px-3 py-2 text-xs font-semibold text-slate-100 shadow-sm transition-colors hover:border-emerald-500 hover:text-emerald-100 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none"
                onclick={addRow}
                disabled={loading || saving}>
                <Plus class="inline h-3.5 w-3.5" />
                Add Include
            </button>
        </div>
    </div>

    {#if loading}
        <div
            class="rounded-md border border-dashed border-slate-800 bg-slate-950/70 p-6 text-center text-sm text-slate-400">
            Loading includes...
        </div>
    {:else}
        <div class="space-y-4">
            <div
                class="rounded-md border border-slate-800 bg-slate-950/80 p-4 shadow-md">
                <div class="flex items-center gap-2">
                    <input
                        class="h-9 w-full rounded-md border border-slate-800/70 bg-slate-900 px-3 text-[13px] text-slate-400 placeholder:text-slate-500 placeholder:opacity-70 focus:outline-none disabled:cursor-not-allowed disabled:opacity-80"
                        value={mappingsUrl}
                        aria-label="Mappings URL"
                        disabled />
                    <div
                        class="inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-slate-400"
                        title="Locked mappings URL"
                        aria-label="Locked mappings URL">
                        <Lock class="h-4 w-4" />
                    </div>
                </div>
            </div>

            {#if rows.length === 0}
                <div
                    class="rounded-md border border-dashed border-slate-800 bg-slate-950/70 p-6 text-center text-sm text-slate-400">
                    No includes configured.
                </div>
            {:else}
                {#each rows as row, index (row.id)}
                    <div
                        class="rounded-md border border-slate-800 bg-slate-950/80 p-4 shadow-md">
                        <div class="flex items-center gap-2">
                            <input
                                class="h-9 w-full rounded-md border border-slate-800/70 bg-slate-900 px-3 text-[13px] text-slate-100 placeholder:text-slate-500 placeholder:opacity-70 focus:border-emerald-500 focus:outline-none"
                                placeholder="/example/path/to/mappings.json or https://example.com/mappings.json"
                                value={row.value}
                                aria-label={`Include ${index + 2}`}
                                oninput={(event) =>
                                    updateRow(
                                        row.id,
                                        (event.currentTarget as HTMLInputElement).value,
                                    )} />
                            <button
                                type="button"
                                class="inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-rose-200 transition-colors hover:border-rose-500 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-rose-200"
                                title="Remove include"
                                onclick={() => removeRow(row.id)}
                                disabled={saving}>
                                <Trash2 class="inline h-4 w-4" />
                            </button>
                        </div>
                    </div>
                {/each}
            {/if}
        </div>
    {/if}

    {#if error}
        <div
            class="rounded-md border border-rose-800 bg-rose-900/40 p-3 text-[12px] text-rose-100">
            {error}
        </div>
    {/if}

    {#snippet footerChildren()}
        <button
            type="button"
            class="inline-flex items-center gap-2 rounded-md border border-slate-700/60 bg-slate-800/60 px-3 py-2 text-[12px] font-semibold text-slate-100 shadow-sm transition-colors hover:border-slate-500 focus:ring-2 focus:ring-slate-500/40 focus:outline-none"
            onclick={loadConfig}
            disabled={loading || saving}>
            <RefreshCcw class={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Reload
        </button>
        <button
            type="button"
            class="inline-flex items-center gap-2 rounded-md border border-emerald-600/60 bg-emerald-600/30 px-3 py-2 text-[12px] font-semibold text-emerald-100 shadow-sm transition-colors hover:bg-emerald-600/40 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            onclick={handleSave}
            disabled={loading || saving}>
            <Save class={`h-4 w-4 ${saving ? "animate-spin" : ""}`} />
            Save
        </button>
    {/snippet}
</Modal>
