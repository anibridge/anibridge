<script lang="ts">
    import { onMount } from "svelte";

    import {
        Activity,
        ArchiveRestore,
        LayoutDashboard,
        List,
        Menu,
        Settings,
        Terminal,
        X,
    } from "@lucide/svelte";
    import { fade } from "svelte/transition";

    import { resolve } from "$app/paths";
    import { page } from "$app/state";
    import logo from "$lib/assets/favicon.svg";
    import GitHubIcon from "$lib/components/icons/github-icon.svelte";

    import "../app.css";

    import { Tooltip } from "bits-ui";

    import ToastHost from "$lib/components/toast-host.svelte";
    import { apiFetch, buildWebSocketUrl } from "$lib/utils/api";

    let { children } = $props();
    let version = $state("?");
    let gitHash = $state("");
    let sidebarOpen = $state(false);
    let ws: WebSocket | null = null;
    let isWsOpen = $state(false);

    function active(href: string, rootMatch = false) {
        const path = page.url.pathname;
        if (href === "/" && rootMatch) return path === "/";
        return href !== "/" && path.startsWith(href);
    }

    async function loadMeta() {
        try {
            const r = await apiFetch("/api/system/meta", undefined, { silent: true });
            if (!r.ok) return;
            const d = await r.json();
            if (d.version) version = d.version;
            if (d.git_hash) gitHash = d.git_hash;
        } catch {
            // toast("Failed to load meta", "warn"); // keep silent by default
        }
    }

    function openWs() {
        try {
            ws?.close();
        } catch {}
        ws = new WebSocket(buildWebSocketUrl("/ws/status"));
        ws.onopen = () => {
            isWsOpen = true;
        };
        ws.onclose = () => {
            isWsOpen = false;
            setTimeout(openWs, 3000);
        };
    }

    onMount(() => {
        loadMeta();
        openWs();
    });
</script>

<svelte:head>
    <title>AniBridge</title>
</svelte:head>

<Tooltip.Provider>
    <div
        class="min-h-dvh overflow-x-hidden bg-[radial-gradient(ellipse_at_top,var(--tw-gradient-stops))] from-slate-950 via-slate-950 to-slate-900 text-slate-100 antialiased selection:bg-blue-600/40 selection:text-white">
        <!-- Toasts -->
        <ToastHost />
        <a
            href="#main"
            class="sr-only bg-blue-600 px-3 py-2 text-sm font-medium text-white shadow-lg focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md"
            >Skip to content</a>
        <!-- Mobile backdrop -->
        {#if sidebarOpen}
            <div
                transition:fade={{ duration: 200 }}
                class="fixed inset-0 z-30 bg-slate-900/70 backdrop-blur-sm lg:hidden"
                role="button"
                aria-label="Close sidebar"
                tabindex="0"
                onclick={() => (sidebarOpen = false)}
                onkeydown={(e) =>
                    (e.key === "Escape" || e.key === "Enter" || e.key === " ") &&
                    (e.preventDefault(), (sidebarOpen = false))}>
            </div>
        {/if}
        <!-- Sidebar -->
        <aside
            class="fixed inset-y-0 left-0 z-40 flex w-64 -translate-x-full flex-col border-r border-slate-800 bg-slate-950/95 px-3 pt-4 pb-6 shadow-xl shadow-slate-950/50 backdrop-blur transition-transform duration-300 ease-out lg:translate-x-0"
            class:translate-x-0={sidebarOpen}>
            <div class="mb-4 flex items-center gap-3 px-2">
                <img
                    src={logo}
                    alt="Logo"
                    class="h-8 w-8"
                    loading="lazy" />
                <a
                    href={resolve("/")}
                    class="group">
                    <h1 class="text-base font-semibold tracking-tight text-white">
                        AniBridge
                    </h1>
                    <p class="text-[11px] tracking-wide text-slate-500 uppercase">
                        Sync Dashboard
                    </p>
                </a>
            </div>
            <nav class="flex flex-1 flex-col gap-1 text-sm font-medium">
                <a
                    href={resolve("/")}
                    class="nav-link {active('/', true) || active('/timeline')
                        ? 'nav-link-active'
                        : ''}"
                    aria-current={active("/", true) || active("/timeline")
                        ? "page"
                        : undefined}
                    ><LayoutDashboard class="inline h-4 w-4" /><span>Dashboard</span
                    ></a>
                <a
                    href={resolve("/mappings")}
                    class="nav-link {active('/mappings') ? 'nav-link-active' : ''}"
                    aria-current={active("/mappings") ? "page" : undefined}
                    ><List class="inline h-4 w-4" /><span>Mappings</span></a>
                <a
                    href={resolve("/logs")}
                    class="nav-link {active('/logs') ? 'nav-link-active' : ''}"
                    aria-current={active("/logs") ? "page" : undefined}
                    ><Terminal class="inline h-4 w-4" /><span>Logs</span></a>
                <a
                    href={resolve("/backups")}
                    class="nav-link {active('/backups') ? 'nav-link-active' : ''}"
                    aria-current={active("/backups") ? "page" : undefined}
                    ><ArchiveRestore class="inline h-4 w-4" /><span>Backups</span></a>
                <div
                    class="mt-4 px-3 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
                    System
                </div>
                <a
                    href={resolve("/settings")}
                    class="nav-link {active('/settings') ? 'nav-link-active' : ''}"
                    aria-current={active("/settings") ? "page" : undefined}
                    ><Settings class="inline h-4 w-4" /><span>Settings</span></a>
                <a
                    href={resolve("/about")}
                    class="nav-link {active('/about') ? 'nav-link-active' : ''}"
                    aria-current={active("/about") ? "page" : undefined}
                    ><Activity class="inline h-4 w-4" /><span>About</span></a>
                <div class="mt-auto border-t border-slate-800/60 pt-4">
                    <p class="px-3 text-[11px] text-slate-500">
                        © {new Date().getFullYear()}
                        <a
                            href="https://anibridge.eliasbenb.dev"
                            target="_blank"
                            rel="noopener"
                            class="transition-colors hover:text-slate-200">AniBridge</a>
                    </p>
                </div>
            </nav>
        </aside>
        <!-- Main content wrapper -->
        <div class="flex min-h-dvh w-full flex-col lg:pl-64">
            <!-- Top bar -->
            <header
                class="sticky top-0 z-20 flex h-14 w-full items-center gap-3 border-b border-slate-800/80 bg-slate-950/80 px-4 pb-[env(safe-area-inset-top)] backdrop-blur supports-backdrop-filter:bg-slate-950/65">
                <button
                    type="button"
                    class="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-700/70 bg-slate-800/70 text-slate-300 hover:bg-slate-700/70 hover:text-white focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:outline-none lg:hidden"
                    aria-label="Toggle navigation"
                    onclick={() => (sidebarOpen = !sidebarOpen)}>
                    {#if sidebarOpen}
                        <X class="inline h-4 w-4" />
                    {:else}
                        <Menu class="inline h-4 w-4" />
                    {/if}
                </button>
                <div
                    class="ml-auto hidden items-center gap-2 rounded-md border border-slate-700/60 bg-slate-900/60 px-3 py-1.5 text-xs text-slate-400 sm:flex"
                    aria-live="polite"
                    title={isWsOpen ? "Live connection established" : "Offline"}>
                    <span class="relative flex h-2 w-2">
                        <span
                            class={`absolute inline-flex h-full w-full rounded-md ${isWsOpen ? "animate-ping bg-blue-500/60" : "bg-amber-500/40"}`}
                        ></span>
                        <span
                            class={`relative inline-flex h-2 w-2 rounded-md ${isWsOpen ? "bg-blue-500" : "bg-amber-500"}`}
                        ></span>
                    </span>
                    <span class="font-medium">{isWsOpen ? "Live" : "Offline"}</span>
                </div>
            </header>
            <!-- Page content area -->
            <main
                id="main"
                class="flex-1 p-4 sm:p-6">
                <div class="fade-in space-y-8">{@render children?.()}</div>
            </main>
            <!-- Footer -->
            <footer
                class="mt-auto border-t border-slate-800/80 bg-slate-950/70 px-4 py-4 pb-[calc(env(safe-area-inset-bottom)+1rem)] text-[11px] text-slate-500 backdrop-blur">
                <div class="flex flex-row items-center justify-between gap-2">
                    <div class="flex flex-wrap items-center gap-3">
                        <a
                            href="https://github.com/anibridge/anibridge"
                            target="_blank"
                            rel="noopener"
                            class="inline-flex items-center gap-1 text-slate-400 transition-colors hover:text-slate-200">
                            <GitHubIcon class="inline h-4 w-4" /><span>GitHub</span>
                        </a>
                        <div>
                            <a
                                href={`https://github.com/anibridge/anibridge/releases/tag/v${version}`}
                                target="_blank"
                                rel="noopener"
                                class="text-slate-600 transition-colors hover:text-slate-200"
                                >v{version}</a>
                            {#if gitHash}
                                <a
                                    href={`https://github.com/anibridge/anibridge/tree/${gitHash}`}
                                    target="_blank"
                                    rel="noopener"
                                    class="ml-1 text-slate-600 transition-colors hover:text-slate-200"
                                    >({gitHash.slice(0, 7)})</a>
                            {/if}
                        </div>
                    </div>
                    <div class="text-slate-600">
                        Made by <a
                            href="https://github.com/eliasbenb"
                            target="_blank"
                            rel="noopener"
                            class="transition-colors hover:text-slate-200"
                            >@eliasbenb</a>
                    </div>
                </div>
            </footer>
        </div>
    </div>
</Tooltip.Provider>
