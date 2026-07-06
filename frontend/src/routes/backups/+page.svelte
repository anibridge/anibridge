<script lang="ts">
    import { onMount } from "svelte";

    import { ArchiveRestore, ChevronRight, Folder } from "@lucide/svelte";
    import { fade } from "svelte/transition";

    import { resolve } from "$app/paths";
    import EmptyState from "$lib/components/empty-state.svelte";
    import PageHeader from "$lib/components/page-header.svelte";
    import Skeleton from "$lib/components/skeleton.svelte";
    import type { StatusResponse } from "$lib/types/api";
    import { apiJson } from "$lib/utils/api";

    let profiles: string[] = $state([]);
    let loading = $state(true);

    async function load() {
        loading = true;
        try {
            const data = await apiJson<StatusResponse>("/api/status");
            profiles = Object.keys(data.profiles || {}).sort();
        } catch (e) {
            console.error(e);
        } finally {
            loading = false;
        }
    }

    onMount(load);
</script>

<div class="space-y-6">
    <PageHeader
        icon={ArchiveRestore}
        title="Backups"
        description="Restore from backups for each profile." />
    {#if loading}
        <div
            class="grid grid-cols-[repeat(auto-fill,minmax(min(100%,32rem),1fr))] gap-4">
            {#each [1, 2, 3, 4] as i (i)}
                <div
                    in:fade={{ duration: 150 }}
                    class="rounded-md border border-(--color-border)/80 bg-(--color-bg-alt)/60 p-4">
                    <Skeleton lines={3} />
                </div>
            {/each}
        </div>
    {:else if !profiles.length}
        <EmptyState
            icon={ArchiveRestore}
            title="No profiles found" />
    {:else}
        <div
            class="grid grid-cols-[repeat(auto-fill,minmax(min(100%,32rem),1fr))] gap-4">
            {#each profiles as p (p)}
                <a
                    href={resolve(`/backups/${p}`)}
                    class="group cursor-pointer rounded-md border border-(--color-border)/80 bg-(--color-bg-alt)/50 p-4 text-left transition-colors hover:bg-(--color-bg-alt)/70 hover:border-(--color-border) focus-visible:ring-2 focus-visible:ring-(--color-accent)/50 focus-visible:outline-none"
                    title={`Open backups for ${p}`}>
                    <div
                        class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div>
                            <div class="flex items-center gap-2">
                                <Folder class="inline h-5 w-5 text-sky-400" />
                                <span class="font-medium text-slate-100">{p}</span>
                            </div>
                            <div class="mt-1 text-xs text-slate-400">
                                Backups for the {p} profile
                            </div>
                        </div>
                        <span
                            class="inline-flex items-center gap-1 self-start rounded-md border border-indigo-600/60 bg-indigo-600/30 px-2 py-1 text-[11px] font-medium text-indigo-200 shadow-sm transition-colors group-hover:bg-indigo-600/40">
                            <span>Open</span>
                            <ChevronRight class="inline h-3 w-3" />
                        </span>
                    </div>
                </a>
            {/each}
        </div>
    {/if}
</div>
