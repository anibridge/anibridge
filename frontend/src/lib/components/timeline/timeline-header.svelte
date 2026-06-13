<script lang="ts">
    import {
        CloudDownload,
        History,
        RefreshCcw,
        RotateCw,
        Wrench,
    } from "@lucide/svelte";
    import { Meter } from "bits-ui";

    import type { CurrentSync } from "$lib/types/api";
    import {
        progressCount,
        progressPercent,
        progressStage,
        progressSubject,
    } from "$lib/utils/sync-progress";

    interface Props {
        profile: string;
        currentSync?: CurrentSync | null;
        isProfileRunning?: boolean;
        isReinitializing?: boolean;
        onFullSync: () => void;
        onPollSync: () => void;
        onReinitialize: () => void;
        onRefresh: () => void;
    }

    let {
        profile,
        currentSync = null,
        isProfileRunning = false,
        isReinitializing = false,
        onFullSync,
        onPollSync,
        onReinitialize,
        onRefresh,
    }: Props = $props();

    const hasRunningSync = () => currentSync?.state === "running";
    const percent = () => progressPercent(currentSync) ?? 0;
    const isDeterminate = () => progressPercent(currentSync) !== null;
</script>

<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
    <div class="flex items-center gap-2">
        <History class="inline h-4 w-4 text-slate-300" />
        <h2 class="text-lg font-semibold">Sync Timeline</h2>
        <span class="text-xs text-slate-500">profile: <i>{profile}</i></span>
    </div>
    <div class="flex items-center gap-2 text-[11px]">
        <button
            onclick={onReinitialize}
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-amber-600/60 bg-amber-600/30 px-2 py-1 font-medium text-amber-200 hover:bg-amber-600/40 disabled:cursor-wait disabled:opacity-70"
            disabled={isReinitializing}
            ><Wrench
                class={`inline h-4 w-4 text-[14px] ${isReinitializing ? "animate-spin" : ""}`} />
            {isReinitializing ? "Reinitializing..." : "Reinitialize"}</button>
        <button
            onclick={onFullSync}
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-emerald-600/60 bg-emerald-600/30 px-2 py-1 font-medium text-emerald-200 hover:bg-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isProfileRunning || isReinitializing}
            ><RefreshCcw class="inline h-4 w-4 text-[14px]" /> Full Scan</button>
        <button
            onclick={onPollSync}
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-sky-600/60 bg-sky-600/30 px-2 py-1 font-medium text-sky-200 hover:bg-sky-600/40 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isProfileRunning || isReinitializing}
            ><CloudDownload class="inline h-4 w-4 text-[14px]" /> Poll Scan</button>
        <button
            onclick={onRefresh}
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-slate-600/60 bg-slate-700/40 px-2 py-1 font-medium text-slate-200 hover:bg-slate-600/50"
            ><RotateCw class="inline h-4 w-4 text-[14px]" /> Refresh</button>
    </div>
</div>
{#if hasRunningSync()}
    <div class="mt-2 space-y-2">
        <div class="flex items-center justify-between text-[11px] text-slate-400">
            <div class="truncate">
                {#if progressSubject(currentSync)}
                    <span class="text-slate-300">{progressSubject(currentSync)}</span>
                    <span class="mx-1">•</span>
                {/if}
                <span class="tracking-wide uppercase"
                    >{progressStage(currentSync)}</span>
            </div>
            <div>
                {progressCount(currentSync)}
            </div>
        </div>
        {#key currentSync?.started_at}
            {#if isDeterminate()}
                <Meter.Root
                    value={percent()}
                    min={0}
                    max={1}
                    class="h-2 w-full overflow-hidden rounded bg-slate-800/80">
                    <div
                        class="h-full bg-linear-to-r from-indigo-500 via-sky-500 to-cyan-400 transition-all duration-300 ease-out"
                        style="transform: translateX(-{100 - 100 * percent()}%)">
                    </div>
                </Meter.Root>
            {:else}
                <div class="h-2 w-full overflow-hidden rounded bg-slate-800/80">
                    <div
                        class="sync-progress-indeterminate h-full w-1/3 bg-linear-to-r from-indigo-500 via-sky-500 to-cyan-400">
                    </div>
                </div>
            {/if}
        {/key}
    </div>
{/if}

<style>
    .sync-progress-indeterminate {
        animation: sync-progress-indeterminate 1.2s ease-in-out infinite;
    }

    @keyframes sync-progress-indeterminate {
        0% {
            transform: translateX(-120%);
        }
        100% {
            transform: translateX(320%);
        }
    }
</style>
