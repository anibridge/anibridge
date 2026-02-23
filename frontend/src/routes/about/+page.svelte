<script lang="ts">
    import { onMount } from "svelte";

    import {
        Activity,
        BookOpen,
        Box,
        ChartColumn,
        Clock3,
        Cpu,
        FileText,
        Github,
        LoaderCircle,
        MessageCircle,
        RefreshCcw,
        ServerCog,
    } from "@lucide/svelte";

    import type { AboutResponse, ProfileStatus } from "$lib/types/api";
    import { apiJson } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    let info: AboutResponse["info"] | null = $state(null);
    let processInfo: AboutResponse["process"] | null = $state(null);
    let scheduler = $state<AboutResponse["scheduler"] | null>(null);
    let loading = $state(true);
    let error: string | null = $state(null);

    async function load() {
        loading = true;
        error = null;
        try {
            const data = await apiJson<AboutResponse>("/api/system/about");
            info = data.info;
            processInfo = data.process;
            scheduler = data.scheduler;
        } catch (e: unknown) {
            error = e instanceof Error ? e.message : String(e);
            toast("Failed to load About info", "error");
        } finally {
            loading = false;
        }
    }

    function profileEntries(): [string, ProfileStatus][] {
        const profiles = scheduler?.profiles ?? {};
        return Object.entries(profiles).sort((a, b) => a[0].localeCompare(b[0]));
    }

    function formatRelative(ts?: string | null): string {
        if (!ts) return "—";
        const d = new Date(ts);
        const diff = Date.now() - d.getTime();
        if (!Number.isFinite(diff)) return "—";
        const sec = Math.floor(diff / 1000);
        if (sec < 45) return "just now";
        const min = Math.floor(sec / 60);
        if (min < 60) return `${min}m ago`;
        const hr = Math.floor(min / 60);
        if (hr < 24) return `${hr}h ago`;
        const day = Math.floor(hr / 24);
        return `${day}d ago`;
    }

    function formatDateTime(ts?: string | null): string {
        if (!ts) return "—";
        try {
            return new Date(ts).toLocaleString();
        } catch {
            return ts;
        }
    }

    function formatMode(mode: string): string {
        return mode
            .split("_")
            .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
            .join(" ");
    }

    function currentSyncLabel(profile: ProfileStatus): string | null {
        const current = profile.status?.current_sync;
        if (!current) return null;
        if (current.stage) return current.stage;
        if (current.state) return current.state;
        return "Running";
    }

    function currentSyncProgress(profile: ProfileStatus): string | null {
        const current = profile.status?.current_sync;
        if (!current) return null;
        const processed = current.section_items_processed ?? 0;
        const total = current.section_items_total ?? 0;
        if (!total) return null;
        return `${processed}/${total} items`;
    }

    onMount(load);
</script>

<div class="space-y-8">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex items-center gap-2">
            <Activity class="inline h-4 w-4 text-slate-300" />
            <h2 class="text-lg font-semibold">About</h2>
        </div>
        <button
            class="inline-flex items-center gap-1 rounded-md border border-slate-800/70 bg-slate-900/60 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-900/80 focus:ring-2 focus:ring-sky-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
            onclick={load}
            disabled={loading}>
            {#if loading}
                <LoaderCircle class="h-3 w-3 animate-spin" />
                <span>Refreshing…</span>
            {:else}
                <RefreshCcw class="h-3 w-3" />
                <span>Refresh</span>
            {/if}
        </button>
    </div>
    {#if error}<p class="text-sm text-rose-400">Failed: {error}</p>{/if}
    <p class="max-w-prose text-xs text-slate-400">
        Diagnostics about the runtime environment, scheduler status, and per-profile
        activity to help with troubleshooting.
    </p>

    <div class="grid gap-4 lg:grid-cols-3">
        <section class="rounded-md border border-slate-800/80 bg-slate-900/50 p-4">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2 text-sm font-medium text-slate-200">
                    <Clock3 class="h-4 w-4 text-sky-400" />
                    <span>Runtime</span>
                </div>
                {#if info?.uptime}<span class="text-[11px] text-slate-400"
                        >Uptime {info.uptime}
                    </span>{/if}
            </div>
            {#if loading}
                <p class="mt-3 text-[11px] text-slate-500">Loading…</p>
            {:else}
                <dl class="mt-3 space-y-2 text-[11px] text-slate-300">
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Version</dt>
                        <dd class="font-medium text-slate-100">
                            {info?.version ?? "—"}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Git Hash</dt>
                        <dd
                            title={info?.git_hash ?? ""}
                            class="font-mono text-[11px] text-slate-100">
                            {info?.git_hash}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Started</dt>
                        <dd class="text-slate-200">
                            {formatDateTime(info?.started_at)}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Updated</dt>
                        <dd class="text-slate-200">{formatDateTime(info?.utc_now)}</dd>
                    </div>
                </dl>
            {/if}
        </section>

        <section class="rounded-md border border-slate-800/80 bg-slate-900/50 p-4">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2 text-sm font-medium text-slate-200">
                    <ServerCog class="h-4 w-4 text-emerald-400" />
                    <span>Scheduler</span>
                </div>
                <span
                    class={`rounded-full px-2 py-0.5 text-[10px] font-semibold tracking-wide uppercase ${
                        scheduler?.running
                            ? "bg-emerald-500/20 text-emerald-200"
                            : "bg-rose-500/20 text-rose-200"
                    }`}>
                    {scheduler?.running ? "Running" : "Idle"}
                </span>
            </div>
            {#if loading}
                <p class="mt-3 text-[11px] text-slate-500">Loading…</p>
            {:else}
                <dl class="mt-3 space-y-2 text-[11px] text-slate-300">
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Configured Profiles</dt>
                        <dd class="text-slate-100">
                            {scheduler?.configured_profiles ?? 0}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Active Profiles</dt>
                        <dd class="text-slate-100">{scheduler?.total_profiles ?? 0}</dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Currently Running</dt>
                        <dd class="text-slate-100">
                            {scheduler?.running_profiles ?? 0}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Syncing Now</dt>
                        <dd class="text-slate-100">
                            {scheduler?.syncing_profiles ?? 0}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Most Recent Sync</dt>
                        <dd class="text-slate-200">
                            {#if scheduler?.most_recent_sync}
                                {formatRelative(scheduler.most_recent_sync)}
                                {#if scheduler?.most_recent_sync_profile}
                                    <span class="text-slate-500">
                                        · {scheduler.most_recent_sync_profile}
                                    </span>
                                {/if}
                            {:else}
                                —
                            {/if}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Next DB Sync</dt>
                        <dd class="text-slate-200">
                            {formatDateTime(scheduler?.next_database_sync_at)}
                        </dd>
                    </div>
                </dl>
                {#if scheduler && Object.keys(scheduler.sync_mode_counts || {}).length > 0}
                    <div class="mt-3 flex flex-wrap gap-2">
                        {#each Object.entries(scheduler.sync_mode_counts) as [mode, count] (mode)}
                            <span
                                class="rounded-md bg-slate-800/80 px-2 py-1 text-[10px] font-medium tracking-wide text-slate-200 uppercase">
                                {formatMode(mode)} · {count}
                            </span>
                        {/each}
                    </div>
                {/if}
            {/if}
        </section>

        <section class="rounded-md border border-slate-800/80 bg-slate-900/50 p-4">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2 text-sm font-medium text-slate-200">
                    <Cpu class="h-4 w-4 text-indigo-400" />
                    <span>Host</span>
                </div>
                <ChartColumn class="h-4 w-4 text-slate-500" />
            </div>
            {#if loading}
                <p class="mt-3 text-[11px] text-slate-500">Loading…</p>
            {:else}
                <dl class="mt-3 space-y-2 text-[11px] text-slate-300">
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">PID</dt>
                        <dd class="font-mono text-[11px] text-slate-100">
                            {processInfo?.pid ?? "—"}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">CPU Cores</dt>
                        <dd class="text-slate-100">{processInfo?.cpu_count ?? "—"}</dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Memory</dt>
                        <dd class="text-slate-200">
                            {#if processInfo?.memory_mb != null}
                                {processInfo.memory_mb.toFixed(2)} MB
                            {:else}
                                —
                            {/if}
                        </dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Python</dt>
                        <dd class="text-slate-100">{info?.python ?? "—"}</dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">SQLite</dt>
                        <dd class="text-slate-100">{info?.sqlite ?? "—"}</dd>
                    </div>
                    <div class="flex items-center justify-between gap-2">
                        <dt class="text-slate-500">Platform</dt>
                        <dd class="text-right break-all text-slate-100">
                            {info?.platform ?? "—"}
                        </dd>
                    </div>
                </dl>
            {/if}
        </section>
    </div>

    <section class="space-y-3">
        <div class="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div class="flex items-center gap-2">
                <Clock3 class="h-4 w-4 text-slate-300" />
                <h3 class="text-base font-semibold">Profile Activity</h3>
            </div>
            {#if scheduler?.most_recent_sync}
                <span class="text-xs text-slate-400">
                    Latest sync {formatRelative(scheduler.most_recent_sync)}
                    {#if scheduler?.most_recent_sync_profile}
                        · {scheduler.most_recent_sync_profile}
                    {/if}
                </span>
            {/if}
        </div>

        {#if loading}
            <div class="flex items-center gap-2 text-xs text-slate-500">
                <LoaderCircle class="h-3 w-3 animate-spin" />
                <span>Loading profiles…</span>
            </div>
        {:else if profileEntries().length === 0}
            <p class="text-sm text-slate-400">
                No profiles are active in the scheduler.
            </p>
        {:else}
            <div
                class="overflow-hidden rounded-md border border-slate-800/70 bg-slate-950/40">
                <table
                    class="min-w-full divide-y divide-slate-800/70 text-left text-[12px] text-slate-200">
                    <thead
                        class="bg-slate-900/60 text-[10px] tracking-wide text-slate-500 uppercase">
                        <tr>
                            <th class="px-4 py-3 font-semibold">Profile</th>
                            <th class="px-4 py-3 font-semibold">Sync Modes</th>
                            <th class="px-4 py-3 font-semibold">Last Sync</th>
                            <th class="px-4 py-3 font-semibold">Status</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800/60">
                        {#each profileEntries() as [name, profile] (name)}
                            <tr class="hover:bg-slate-900/40">
                                <td class="px-4 py-3">
                                    <div class="font-medium text-slate-100">{name}</div>
                                    {#if profile.config?.library_user}
                                        <div class="text-[11px] text-slate-400">
                                            {profile.config.library_namespace} · {profile
                                                .config.library_user}
                                        </div>
                                    {/if}
                                    {#if profile.config?.list_user}
                                        <div class="text-[11px] text-slate-500">
                                            {profile.config.list_namespace} · {profile
                                                .config.list_user}
                                        </div>
                                    {/if}
                                </td>
                                <td class="px-4 py-3">
                                    <div class="flex flex-wrap gap-1">
                                        {#if profile.config?.scan_modes?.length}
                                            {#each profile.config.scan_modes as mode (mode)}
                                                <span
                                                    class="rounded-md bg-slate-800/80 px-2 py-0.5 text-[10px] font-medium tracking-wide text-slate-200 uppercase">
                                                    {formatMode(mode)}
                                                </span>
                                            {/each}
                                        {:else}
                                            <span class="text-slate-500"
                                                >Single run</span>
                                        {/if}
                                    </div>
                                </td>
                                <td class="px-4 py-3">
                                    <div
                                        class="text-slate-200"
                                        title={formatDateTime(
                                            profile.status?.last_synced,
                                        )}>
                                        {formatRelative(profile.status?.last_synced)}
                                    </div>
                                    <div class="text-[11px] text-slate-500">
                                        {formatDateTime(profile.status?.last_synced)}
                                    </div>
                                </td>
                                <td class="px-4 py-3">
                                    {#if profile.status?.current_sync}
                                        <div
                                            class="rounded-md bg-amber-500/15 px-2 py-1 text-[11px] font-medium text-amber-200">
                                            {currentSyncLabel(profile)}
                                            {#if profile.status.current_sync.section_title}
                                                <span class="text-amber-300">
                                                    · {profile.status.current_sync
                                                        .section_title}
                                                </span>
                                            {/if}
                                        </div>
                                        {#if currentSyncProgress(profile)}
                                            <div
                                                class="mt-1 text-[10px] text-slate-400">
                                                {currentSyncProgress(profile)}
                                            </div>
                                        {/if}
                                    {:else}
                                        <span
                                            class={`rounded-md px-2 py-1 text-[11px] font-medium ${
                                                profile.status?.running
                                                    ? "bg-emerald-500/15 text-emerald-200"
                                                    : "bg-slate-800/80 text-slate-300"
                                            }`}>
                                            {profile.status?.running
                                                ? "Ready"
                                                : "Stopped"}
                                        </span>
                                    {/if}
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        {/if}
    </section>

    <section class="space-y-3">
        <div class="flex items-center gap-2">
            <BookOpen class="h-4 w-4 text-slate-300" />
            <h3 class="text-base font-semibold">Project Resources</h3>
        </div>
        <p class="text-xs text-slate-400">
            Deep dive into AniBridge docs, releases, and support channels.
        </p>
        <div class="grid gap-3 md:grid-cols-2">
            <a
                href="https://github.com/anibridge/anibridge"
                target="_blank"
                rel="noreferrer noopener"
                class="group flex h-full items-stretch gap-3 rounded-md border border-slate-800/70 bg-slate-900/50 p-4 transition-colors hover:bg-slate-900/70">
                <div
                    class="flex h-full w-16 items-center justify-center rounded-md bg-slate-800/50 text-slate-300 transition-colors group-hover:bg-slate-800/70">
                    <Github class="h-6 w-6" />
                </div>
                <div class="flex flex-1 flex-col justify-center text-left">
                    <div class="text-sm font-semibold text-slate-100">
                        GitHub Repository
                    </div>
                    <p class="mt-1 text-xs text-slate-400">
                        Source code, issue tracking, and release notes.
                    </p>
                    <div class="mt-2 text-[11px] text-slate-500">
                        github.com/anibridge/anibridge
                    </div>
                </div>
            </a>
            <a
                href="https://ghcr.io/anibridge/anibridge"
                target="_blank"
                rel="noreferrer noopener"
                class="group flex h-full items-stretch gap-3 rounded-md border border-slate-800/70 bg-slate-900/50 p-4 transition-colors hover:bg-slate-900/70">
                <div
                    class="flex h-full w-16 items-center justify-center rounded-md bg-slate-800/50 text-slate-300 transition-colors group-hover:bg-slate-800/70">
                    <Box class="h-6 w-6" />
                </div>
                <div class="flex flex-1 flex-col justify-center text-left">
                    <div class="text-sm font-semibold text-slate-100">
                        Docker Repository
                    </div>
                    <p class="mt-1 text-xs text-slate-400">
                        Prebuilt container images.
                    </p>
                    <div class="mt-2 text-[11px] text-slate-500">
                        ghcr.io/anibridge/anibridge
                    </div>
                </div>
            </a>
            <a
                href="https://anibridge.eliasbenb.dev"
                target="_blank"
                rel="noreferrer noopener"
                class="group flex h-full items-stretch gap-3 rounded-md border border-slate-800/70 bg-slate-900/50 p-4 transition-colors hover:bg-slate-900/70">
                <div
                    class="flex h-full w-16 items-center justify-center rounded-md bg-slate-800/50 text-slate-300 transition-colors group-hover:bg-slate-800/70">
                    <FileText class="h-6 w-6" />
                </div>
                <div class="flex flex-1 flex-col justify-center text-left">
                    <div class="text-sm font-semibold text-slate-100">
                        Documentation
                    </div>
                    <p class="mt-1 text-xs text-slate-400">
                        Installation guides, configuration examples, and FAQs.
                    </p>
                    <div class="mt-2 text-[11px] text-slate-500">
                        anibridge.eliasbenb.dev
                    </div>
                </div>
            </a>
            <a
                href="https://discord.gg/ey8kyQU9aD"
                target="_blank"
                rel="noreferrer noopener"
                class="group flex h-full items-stretch gap-3 rounded-md border border-slate-800/70 bg-slate-900/50 p-4 transition-colors hover:bg-slate-900/70">
                <div
                    class="flex h-full w-16 items-center justify-center rounded-md bg-slate-800/50 text-slate-300 transition-colors group-hover:bg-slate-800/70">
                    <MessageCircle class="h-6 w-6" />
                </div>
                <div class="flex flex-1 flex-col justify-center text-left">
                    <div class="text-sm font-semibold text-slate-100">
                        Discord Community
                    </div>
                    <p class="mt-1 text-xs text-slate-400">
                        Community support and announcements.
                    </p>
                    <div class="mt-2 text-[11px] text-slate-500">
                        discord.gg/ey8kyQU9aD
                    </div>
                </div>
            </a>
        </div>
    </section>
</div>
