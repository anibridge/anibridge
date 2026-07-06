<script lang="ts">
    import { onMount } from "svelte";

    import { ArchiveRestore, Eye, LoaderCircle, RefreshCcw } from "@lucide/svelte";
    import { fade } from "svelte/transition";

    import JsonCodeBlock from "$lib/components/json-code-block.svelte";
    import PageHeader from "$lib/components/page-header.svelte";
    import Skeleton from "$lib/components/skeleton.svelte";
    import type { BackupMeta } from "$lib/types/api";
    import Modal from "$lib/ui/modal.svelte";
    import { apiJson } from "$lib/utils/api";
    import { humanDuration, humanSize } from "$lib/utils/human";
    import { toast } from "$lib/utils/notify";

    const { params } = $props<{ params: { profile: string } }>();

    let backups: BackupMeta[] = $state([]);
    let loading = $state(true);
    let restoring = $state<string | null>(null);
    let previewing = $state<string | null>(null);
    interface BackupRawPreview {
        user?: unknown;
        lists?: unknown;
        [k: string]: unknown; // flexible structure
    }
    let previewCache = $state<Record<string, BackupRawPreview>>({});
    let previewLoading = $state<string | null>(null);

    async function load() {
        loading = true;
        try {
            backups = await listBackups(params.profile);
        } catch (e) {
            console.error(e);
        } finally {
            loading = false;
        }
    }

    async function listBackups(profile: string): Promise<BackupMeta[]> {
        const data = await apiJson<{ backups: BackupMeta[] }>(
            `/api/backups/${profile}`,
        );
        return data.backups || [];
    }

    async function restoreBackup(profile: string, filename: string) {
        await apiJson(`/api/backups/${profile}/restore`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename }),
        });
    }

    async function openPreview(filename: string) {
        if (previewing === filename) return;
        if (!previewCache[filename]) {
            previewLoading = filename;
            try {
                const data = await apiJson<BackupRawPreview>(
                    `/api/backups/${params.profile}/raw/${filename}`,
                );
                previewCache[filename] = data;
            } catch (e) {
                console.error(e);
                toast("Failed to load preview", "error");
                previewLoading = null;
                return;
            }
            previewLoading = null;
        }
        previewing = filename;
    }

    function getPreviewOpen() {
        return previewing !== null;
    }
    function setPreviewOpen(open: boolean) {
        if (!open) previewing = null;
    }

    async function doRestore(filename: string) {
        if (
            !confirm(
                `Restore '${filename}'? This overwrites current AniList list entries.`,
            )
        )
            return;
        restoring = filename;
        try {
            await restoreBackup(params.profile, filename);
        } catch (e) {
            console.error(e);
            toast("Restore failed", "error");
        } finally {
            restoring = null;
        }
    }

    onMount(load);
</script>

<div class="space-y-6">
    <PageHeader
        icon={ArchiveRestore}
        title="Backups"
        description={`Profile: ${params.profile}`}>
        {#snippet actions()}
            <button
                onclick={load}
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-(--color-border) bg-(--color-surface)/70 px-2 py-1 font-medium text-(--color-muted-foreground) transition-colors hover:bg-(--color-surface-alt) hover:text-(--color-foreground)">
                <RefreshCcw class="inline h-4 w-4 text-[14px]" /> Refresh
            </button>
        {/snippet}
    </PageHeader>

    {#if loading}
        <div
            in:fade={{ duration: 150 }}
            class="rounded-md border border-(--color-border)/80 bg-(--color-bg-alt)/60 p-4">
            <Skeleton lines={6} />
        </div>
    {:else if !backups.length}
        <p class="text-sm text-slate-500">No backups found.</p>
    {:else}
        <div
            class="overflow-x-auto rounded-md border border-slate-800 bg-slate-900/60 shadow-sm backdrop-blur-sm">
            <table class="w-full text-[11px]">
                <thead
                    class="bg-(--color-surface-alt)/60 text-left text-[11px] tracking-wide text-(--color-dimmed) uppercase">
                    <tr>
                        <th class="px-3 py-2 font-medium">Filename</th>
                        <th class="px-3 py-2 font-medium">Created</th>
                        <th class="px-3 py-2 font-medium">Age</th>
                        <th class="px-3 py-2 font-medium">Size</th>
                        <th class="px-3 py-2 font-medium">User</th>
                        <th class="px-3 py-2 text-right font-medium">Actions</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-slate-800/70">
                    {#each backups as b (b.filename)}
                        <tr class="transition hover:bg-slate-800/50">
                            <td class="px-3 py-2 font-mono text-[11px] break-all"
                                >{b.filename}</td>
                            <td class="px-3 py-2 text-[11px]"
                                >{new Date(b.created_at).toLocaleString()}</td>
                            <td class="px-3 py-2 text-slate-400"
                                >{humanDuration(b.age_seconds)}</td>
                            <td class="px-3 py-2 text-slate-400"
                                >{humanSize(b.size_bytes)}</td>
                            <td class="px-3 py-2 text-slate-400">{b.user || "—"}</td>
                            <td class="px-3 py-2">
                                <div class="flex justify-end gap-2">
                                    <button
                                        class="inline-flex items-center gap-1 rounded-md border border-sky-600/60 bg-sky-600/30 px-2 py-1 text-[11px] font-medium text-sky-100 hover:bg-sky-600/40 disabled:opacity-50"
                                        disabled={previewLoading === b.filename}
                                        onclick={() => openPreview(b.filename)}
                                        title="Preview raw JSON">
                                        {#if previewLoading === b.filename}
                                            <LoaderCircle
                                                class="inline h-4 w-4 animate-spin" />
                                        {:else}
                                            <Eye class="inline h-4 w-4" />
                                        {/if}
                                        <span>Preview</span>
                                    </button>
                                    <button
                                        class="inline-flex items-center gap-1 rounded-md border border-emerald-600/60 bg-emerald-600/30 px-2 py-1 text-[11px] font-medium text-emerald-100 hover:bg-emerald-600/40 disabled:opacity-50"
                                        disabled={restoring === b.filename}
                                        onclick={() => doRestore(b.filename)}
                                        title="Restore backup">
                                        {#if restoring === b.filename}
                                            <LoaderCircle
                                                class="inline h-4 w-4 animate-spin" />
                                        {:else}
                                            <ArchiveRestore class="inline h-4 w-4" />
                                        {/if}
                                        <span>Restore</span>
                                    </button>
                                </div>
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    {/if}

    {#if previewing}
        <Modal
            bind:open={getPreviewOpen, setPreviewOpen}
            contentClass="fixed top-1/2 left-1/2 z-50 w-full max-w-3xl -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-md border border-slate-800 bg-slate-950/80 shadow-xl backdrop-blur-md"
            headerWrapperClass="border-b border-slate-800/80 bg-slate-900/60 px-4 py-2"
            footerClass="flex items-center justify-end gap-2 border-t border-slate-800/70 bg-slate-900/60 px-4 py-2"
            closeButtonClass="rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-slate-800/70 hover:text-slate-200"
            onOpenAutoFocus={(e: Event) => e.preventDefault()}
            onCloseAutoFocus={() => (previewing = null)}>
            {#snippet titleChildren()}
                <div class="text-sm font-semibold tracking-wide text-slate-200">
                    Backup Preview
                    <span class="ml-1 font-mono text-[10px] text-slate-400"
                        >{previewing}</span>
                </div>
            {/snippet}
            {#snippet footerChildren()}
                <div>
                    <button
                        class="rounded-md border border-(--color-border) bg-(--color-surface)/70 px-3 py-1 text-[11px] font-medium text-(--color-muted-foreground) transition-colors hover:bg-(--color-surface-alt) hover:text-(--color-foreground)"
                        onclick={() => setPreviewOpen(false)}>
                        Close
                    </button>
                </div>
            {/snippet}
            <div class="p-4">
                <JsonCodeBlock
                    value={previewCache[previewing] || {}}
                    class="max-h-[70vh] text-[11px] leading-snug" />
            </div>
        </Modal>
    {/if}
</div>

<style>
    td,
    th {
        white-space: nowrap;
    }
</style>
