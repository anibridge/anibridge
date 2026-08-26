<script lang="ts">
    import { onMount } from "svelte";

    import { ArchiveRestore, ChevronRight, Folder } from "@lucide/svelte";

    import { resolve } from "$app/paths";
    import type { StatusResponse } from "$lib/types/api";
    import { apiJson } from "$lib/utils/api";

    let profiles: string[] = $state([]);
    let loading = $state(true);
    let loadError: string | null = $state(null);

    async function load() {
        loading = true;
        loadError = null;
        try {
            const data = await apiJson<StatusResponse>("/api/status");
            profiles = Object.keys(data.profiles || {}).sort();
        } catch (e) {
            console.error(e);
            loadError = e instanceof Error ? e.message : "Failed to load profiles";
        } finally {
            loading = false;
        }
    }

    onMount(load);
</script>

<div class="space-y-6">
    <div class="space-y-1">
        <div class="flex items-center gap-2">
            <ArchiveRestore class="inline h-4 w-4 text-slate-300" />
            <h2 class="text-lg font-semibold">Backups</h2>
            <span class="hidden text-xs text-slate-500 sm:inline"
                >{profiles.length} profiles</span>
        </div>
        <p class="text-xs text-slate-400">Restore from backups for each profile.</p>
    </div>
    {#if loading}
        <div
            class="grid grid-cols-[repeat(auto-fill,minmax(min(100%,32rem),1fr))] gap-4">
            {#each [1, 2, 3, 4] as i (i)}
                <div
                    class="animate-pulse rounded-md border border-slate-800/60 bg-slate-900/40 p-4">
                    <div
                        class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"
                        aria-hidden="true">
                        <div>
                            <div class="flex items-center gap-2">
                                <div class="h-5 w-5 rounded bg-slate-700/60"></div>
                                <div class="h-4 w-32 rounded bg-slate-700/50"></div>
                            </div>
                            <div class="mt-1 h-3 w-20 rounded bg-slate-800/60"></div>
                        </div>

                        <div class="self-start">
                            <div class="h-6 w-16 rounded-md bg-indigo-700/30"></div>
                        </div>
                    </div>
                </div>
            {/each}
        </div>
    {:else if loadError}
        <p
            class="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
            Failed to load profiles: {loadError}
        </p>
    {:else if !profiles.length}
        <p class="text-sm text-slate-500">No profiles found.</p>
    {:else}
        <div
            class="grid grid-cols-[repeat(auto-fill,minmax(min(100%,32rem),1fr))] gap-4">
            {#each profiles as p (p)}
                <a
                    href={resolve(`/backups/${p}`)}
                    class="group cursor-pointer rounded-md border border-slate-800/80 bg-slate-900/50 p-4 text-left transition-colors hover:bg-slate-900/70 focus:ring-2 focus:ring-sky-600/40 focus:outline-none"
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
