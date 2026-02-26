<script lang="ts">
    import { onMount } from "svelte";

    import {
        Info,
        Languages,
        LoaderCircle,
        RefreshCw,
        Save,
        Settings as SettingsIcon,
        TriangleAlert,
    } from "@lucide/svelte";

    import YamlEditor from "$lib/components/code-editor/yaml-editor.svelte";
    import type {
        ConfigDocumentResponse,
        ConfigDocumentUpdateRequest,
        ConfigUpdateResponse,
    } from "$lib/types/api";
    import {
        anilistTitleLang,
        setAniListTitleLang,
        type TitleLanguage,
    } from "$lib/utils/anilist";
    import { apiFetch, apiJson } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";

    let loading = $state(true);
    let saving = $state(false);
    let restarting = $state(false);
    let restartNotice: string | null = $state(null);
    let loadError: string | null = $state(null);
    let saveError: string | null = $state(null);
    let configAccessBlocked = $state(false);
    let configPath = $state("");
    let fileExists = $state(false);
    let editorValue = $state("");
    let initialValue = $state("");
    let configSchema = $state<Record<string, unknown> | null>(null);
    let mtime: number | null = null;

    const hasChanges = $derived(editorValue !== initialValue);

    let restartPollGeneration = 0;

    onMount(() => {
        void loadConfig();
        return () => {
            restartPollGeneration += 1;
        };
    });

    async function waitForServerAndRefresh() {
        const generation = ++restartPollGeneration;
        const deadline = Date.now() + 90_000;

        while (Date.now() < deadline) {
            if (generation !== restartPollGeneration) return;

            await new Promise((resolve) => setTimeout(resolve, 2_000));

            if (generation !== restartPollGeneration) return;

            try {
                const response = await apiFetch("/api/system/meta", undefined, {
                    silent: true,
                });
                if (!response.ok) continue;

                restarting = false;
                restartNotice = null;
                toast("AniBridge is back online.", "success");
                await loadConfig();
                return;
            } catch {}
        }

        restarting = false;
        restartNotice = "Restart is taking longer than expected. Use Reload to retry.";
    }

    async function loadConfig() {
        loading = true;
        loadError = null;
        saveError = null;
        configAccessBlocked = false;
        try {
            const response = await apiFetch("/api/config", undefined, { silent: true });
            const payload = await response.json();

            if (!response.ok) {
                if (response.status === 403) {
                    configAccessBlocked = true;
                    loadError =
                        "Config API access is disabled because web authentication " +
                        "is not configured.";
                } else {
                    loadError = formatApiError(payload, response.status);
                }
                return;
            }

            const configPayload = payload as ConfigDocumentResponse;
            configPath = configPayload.config_path;
            fileExists = configPayload.file_exists;
            editorValue = configPayload.content ?? "";
            initialValue = editorValue;
            configSchema = configPayload.schema ?? null;
            mtime = configPayload.mtime ?? null;
        } catch (error) {
            loadError = formatError(error);
        } finally {
            loading = false;
        }
    }

    async function saveConfig() {
        if (saving || loading || configAccessBlocked) return;
        saveError = null;

        const payload: ConfigDocumentUpdateRequest = {
            content: editorValue,
            expected_mtime: mtime ?? undefined,
        };

        saving = true;
        try {
            await apiJson<ConfigUpdateResponse>("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            toast(
                "Configuration saved. Restart AniBridge to apply changes.",
                "success",
            );
            await loadConfig();
        } catch (error) {
            saveError = formatError(error);
        } finally {
            saving = false;
        }
    }

    async function restartServer() {
        if (restarting || saving || loading) return;

        const confirmed = window.confirm(
            "Restart AniBridge now? The web UI will be briefly unavailable.",
        );
        if (!confirmed) return;

        restarting = true;
        saveError = null;
        restartNotice = "Restart requested. Waiting for AniBridge to come back...";
        try {
            const response = await apiFetch("/api/system/restart", { method: "POST" });

            if (!response.ok) {
                const payload = await response.json().catch(() => null);
                saveError = formatApiError(payload, response.status);
                restarting = false;
                restartNotice = null;
                return;
            }

            toast("Restart requested. AniBridge will return in a few.", "success");
            void waitForServerAndRefresh();
        } catch (error) {
            const msg = formatError(error);
            if (/network|failed to fetch|load failed/i.test(msg)) {
                toast("Restart in progress. Waiting for server to come back.", "info");
                void waitForServerAndRefresh();
                return;
            }

            saveError = msg;
            restarting = false;
            restartNotice = null;
        }
    }

    function revertChanges() {
        editorValue = initialValue;
        saveError = null;
    }

    function formatError(error: unknown): string {
        if (error instanceof Error) return error.message;
        if (typeof error === "string") return error;
        return "Unexpected error";
    }

    function formatApiError(payload: unknown, statusCode: number): string {
        if (
            payload &&
            typeof payload === "object" &&
            "detail" in payload &&
            typeof payload.detail === "string"
        ) {
            return payload.detail;
        }
        if (payload && typeof payload === "object" && "error" in payload) {
            const err = payload.error;
            if (typeof err === "string" && err.trim()) {
                return err;
            }
        }
        return `Request failed (${statusCode})`;
    }

    const LANG_OPTS: TitleLanguage[] = ["romaji", "english", "native"];

    function setLang(v: TitleLanguage) {
        setAniListTitleLang(v);
        toast(`AniList title language set to ${v}`, "success");
    }
</script>

<div class="space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-2 text-slate-200">
            <SettingsIcon class="h-5 w-5 text-slate-400" />
            <div>
                <h2 class="text-base font-semibold">Configuration</h2>
                <p class="text-xs text-slate-500">
                    Edit your AniBridge configuration file directly.
                </p>
            </div>
        </div>
        <div class="flex flex-wrap gap-2 text-xs">
            <button
                type="button"
                class="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900/60 px-3 py-1 text-slate-100 hover:bg-slate-800/60"
                onclick={loadConfig}
                disabled={loading || saving || restarting}>
                <RefreshCw class="h-3.5 w-3.5" /> Reload
            </button>
            <a
                href="https://anibridge.eliasbenb.dev"
                target="_blank"
                rel="noreferrer"
                class="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900/60 px-3 py-1 text-slate-100 hover:bg-slate-800/60">
                <Info class="h-3.5 w-3.5" /> Docs
            </a>
            <button
                type="button"
                class="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900/60 px-3 py-1 text-slate-100 hover:bg-slate-800/60 disabled:opacity-50"
                onclick={revertChanges}
                disabled={loading ||
                    saving ||
                    restarting ||
                    configAccessBlocked ||
                    !hasChanges}>
                Revert
            </button>
            <button
                type="button"
                class="inline-flex items-center gap-1 rounded border border-amber-500 bg-amber-600 px-4 py-1 font-semibold text-white hover:bg-amber-500 disabled:opacity-50"
                onclick={restartServer}
                disabled={loading || saving || restarting}>
                <RefreshCw class={`h-3.5 w-3.5 ${restarting ? "animate-spin" : ""}`} />
                {restarting ? "Restarting…" : "Restart Server"}
            </button>
            <button
                type="button"
                class="inline-flex items-center gap-1 rounded border border-blue-500 bg-blue-600 px-4 py-1 font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
                onclick={saveConfig}
                disabled={loading ||
                    saving ||
                    restarting ||
                    configAccessBlocked ||
                    !hasChanges}>
                <Save class="h-3.5 w-3.5" />
                {saving ? "Saving…" : "Save"}
            </button>
        </div>
    </div>

    <div
        class="rounded border border-slate-800 bg-slate-950/60 p-4 text-xs text-slate-300">
        <p>
            <span class="font-semibold text-slate-100">Configuration file:</span>
            <code
                class="ml-2 rounded bg-slate-900 px-1 py-0.5 text-[11px] text-slate-200">
                {configPath || "(not set)"}
            </code>
        </p>
        <p class="mt-1 text-slate-500">
            {#if fileExists}
                The existing file will be overwritten when you save.
            {:else}
                A new configuration file will be created when you save.
            {/if}
        </p>
    </div>

    {#if loadError}
        <div
            class="flex items-center gap-2 rounded border border-rose-900/60 bg-rose-950/60 px-3 py-2 text-xs text-rose-100">
            <TriangleAlert class="h-3.5 w-3.5" /> Failed to load configuration: {loadError}
        </div>
    {/if}

    {#if configAccessBlocked}
        <div
            class="rounded border border-amber-900/70 bg-amber-950/50 px-3 py-2 text-xs text-amber-100">
            <p class="font-medium">Configuration editor is blocked.</p>
            <p class="mt-1 text-amber-200/90">
                Configure <code class="rounded bg-amber-900/40 px-1"
                    >web.basic_auth</code>
                or explicitly set
                <code class="rounded bg-amber-900/40 px-1"
                    >web.allow_config_without_auth: true</code>
                to allow unauthenticated access.
            </p>
        </div>
    {/if}

    {#if saveError}
        <div
            class="flex items-center gap-2 rounded border border-rose-900/60 bg-rose-950/60 px-3 py-2 text-xs text-rose-100">
            <TriangleAlert class="h-3.5 w-3.5" />
            {saveError}
        </div>
    {/if}

    {#if restarting || restartNotice}
        <div
            class="flex items-center gap-2 rounded border border-amber-900/70 bg-amber-950/50 px-3 py-2 text-xs text-amber-100">
            <LoaderCircle class={`h-3.5 w-3.5 ${restarting ? "animate-spin" : ""}`} />
            {restartNotice ?? "Restarting AniBridge..."}
        </div>
    {/if}

    <div class="space-y-2">
        <div class="flex items-center gap-2 text-slate-300">
            <Info class="h-4 w-4 text-slate-500" />
            <p class="text-xs text-slate-400">
                Paste or edit the YAML content below. Saving replaces the existing file
                and requires restarting AniBridge to apply changes.
            </p>
        </div>
        <div
            class="rounded-lg border border-slate-800 bg-slate-950/70 p-2 shadow-inner">
            {#if loading}
                <div
                    class="flex items-center justify-center gap-2 py-32 text-xs text-slate-400">
                    <LoaderCircle class="h-4 w-4 animate-spin" /> Loading configuration…
                </div>
            {:else}
                <div
                    class="h-130 min-h-70 overflow-hidden rounded-md border border-slate-900/80">
                    <YamlEditor
                        bind:value={editorValue}
                        theme="dark"
                        fontSize="13px"
                        readOnly={saving || configAccessBlocked}
                        schemaObject={configSchema ?? undefined}
                        fileUri={configPath ? `file://${configPath}` : undefined} />
                </div>
            {/if}
        </div>
        {#if hasChanges}
            <p class="text-[11px] text-slate-400">
                Unsaved changes detected. Save to persist updates.
            </p>
        {/if}
    </div>

    <div class="space-y-2">
        <h4
            class="flex items-center gap-2 text-sm font-medium tracking-wide text-slate-200">
            <Languages class="inline h-4 w-4 text-slate-400" /> AniList Title Language
        </h4>
        <p class="text-[11px] leading-relaxed text-slate-500">
            Choose which title language to prefer. Stored only in this browser.
        </p>
        <div class="flex flex-wrap gap-2">
            {#each LANG_OPTS as opt (opt)}
                <button
                    type="button"
                    onclick={() => setLang(opt)}
                    class={`rounded-md border px-3 py-1.5 text-[11px] font-medium ${$anilistTitleLang === opt ? "border-blue-500 bg-blue-600 text-white" : "border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-700/60"}`}
                    >{opt[0].toUpperCase() + opt.slice(1)}</button>
            {/each}
        </div>
        <p class="text-[10px] text-slate-500">
            Current preference:
            <span class="font-medium text-slate-300">
                {$anilistTitleLang === "userPreferred"
                    ? "AniList Preferred"
                    : $anilistTitleLang}
            </span>
        </p>
    </div>
</div>
