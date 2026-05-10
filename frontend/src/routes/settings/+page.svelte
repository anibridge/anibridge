<script lang="ts">
    import { onMount } from "svelte";

    import {
        FileCodeCorner,
        Info,
        Languages,
        LoaderCircle,
        RefreshCw,
        Save,
        Settings as SettingsIcon,
        TriangleAlert,
    } from "@lucide/svelte";
    import { Tabs } from "bits-ui";

    import YamlEditor from "$lib/components/code-editor/yaml-editor.svelte";
    import {
        deepClone,
        humanizeKey,
        removeAtPath,
        setAtPath,
        stableStringify,
    } from "$lib/components/config/schema-form";
    import SchemaFormNode from "$lib/components/config/schema-form-node.svelte";
    import type {
        ConfigDocumentResponse,
        ConfigDocumentUpdateRequest,
        ConfigStructuredUpdateRequest,
        ConfigUpdateResponse,
    } from "$lib/types/api";
    import {
        anilistTitleLang,
        setAniListTitleLang,
        type TitleLanguage,
    } from "$lib/utils/anilist";
    import { apiFetch, apiJson, buildAppPath } from "$lib/utils/api";
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
    let uiValue = $state<Record<string, unknown>>({});
    let initialUiValue = $state<Record<string, unknown>>({});
    let uiSettingsError: string | null = $state(null);
    let mtime: number | null = null;
    let activeTab = $state<"ui" | "yaml">("ui");
    let activeUiSection = $state<"app" | "global" | "profiles">("app");

    const hasYamlChanges = $derived(editorValue !== initialValue);
    const hasUiChanges = $derived(
        stableStringify(uiValue) !== stableStringify(initialUiValue),
    );
    const hasChanges = $derived(activeTab === "yaml" ? hasYamlChanges : hasUiChanges);
    const uiEditorAvailable = $derived(configSchema !== null && !uiSettingsError);
    const topLevelSchemaProperties = $derived(
        (configSchema?.properties as Record<string, unknown> | undefined) ?? {},
    );
    const globalSettingsSchema = $derived(
        (topLevelSchemaProperties.global_config as
            | Record<string, unknown>
            | undefined) ?? null,
    );
    const profilesSchema = $derived(
        (topLevelSchemaProperties.profiles as Record<string, unknown> | undefined) ??
            null,
    );
    const appSettingEntries = $derived(
        Object.entries(topLevelSchemaProperties).filter(
            ([fieldName]) => fieldName !== "global_config" && fieldName !== "profiles",
        ),
    );

    let restartPollGeneration = 0;

    onMount(() => {
        void loadConfig();
        return () => {
            restartPollGeneration += 1;
        };
    });

    async function fetchWithTimeout(
        input: RequestInfo | URL,
        init: RequestInit = {},
        timeoutMs = 5_000,
    ): Promise<Response> {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), timeoutMs);
        try {
            return await fetch(input, {
                ...init,
                cache: "no-store",
                signal: controller.signal,
            });
        } finally {
            clearTimeout(timeout);
        }
    }

    async function checkServerHealth(): Promise<boolean> {
        const response = await fetchWithTimeout(
            buildAppPath(`/livez?t=${Date.now()}`),
            { headers: { Accept: "application/json" } },
        );
        if (!response.ok) return false;

        const payload = (await response.json().catch(() => null)) as {
            status?: string;
        } | null;
        return payload?.status === "ok";
    }

    async function waitForServerAndRefresh() {
        const generation = ++restartPollGeneration;
        const deadline = Date.now() + 90_000;

        while (Date.now() < deadline) {
            if (generation !== restartPollGeneration) return;

            await new Promise((resolve) => setTimeout(resolve, 2_000));

            if (generation !== restartPollGeneration) return;

            try {
                const healthy = await checkServerHealth();
                if (!healthy) continue;

                restarting = false;
                restartNotice = null;
                toast("AniBridge is back online.", "success");
                window.location.reload();
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
            uiValue = deepClone(configPayload.settings ?? {});
            initialUiValue = deepClone(configPayload.settings ?? {});
            uiSettingsError = configPayload.settings_error ?? null;
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
            const response = await apiJson<ConfigUpdateResponse>("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            toast(
                response.requires_restart
                    ? "Configuration saved. A restart is required to apply all changes."
                    : "Configuration saved and applied.",
                "success",
            );
            await loadConfig();
        } catch (error) {
            saveError = formatError(error);
        } finally {
            saving = false;
        }
    }

    async function saveUiConfig() {
        if (saving || loading || configAccessBlocked || !uiEditorAvailable) return;
        saveError = null;

        const payload: ConfigStructuredUpdateRequest = {
            settings: uiValue,
            expected_mtime: mtime ?? undefined,
        };

        saving = true;
        try {
            const response = await apiJson<ConfigUpdateResponse>(
                "/api/config/structured",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                },
            );
            toast(
                response.requires_restart
                    ? "Configuration saved. A restart is required to apply all changes."
                    : "Configuration saved and applied.",
                "success",
            );
            await loadConfig();
        } catch (error) {
            saveError = formatError(error);
        } finally {
            saving = false;
        }
    }

    async function handleSave() {
        if (activeTab === "yaml") {
            await saveConfig();
            return;
        }

        if (
            !window.confirm(
                "Saving through the guided editor rewrites the YAML document and discards all comments. Continue?",
            )
        ) {
            return;
        }

        await saveUiConfig();
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
        if (activeTab === "yaml") {
            editorValue = initialValue;
        } else {
            uiValue = deepClone(initialUiValue);
        }
        saveError = null;
    }

    function resetDrafts() {
        editorValue = initialValue;
        uiValue = deepClone(initialUiValue);
        saveError = null;
    }

    function switchTab(nextTab: string) {
        if (nextTab !== "ui" && nextTab !== "yaml") return;
        if (nextTab === activeTab) return;

        const dirty = hasYamlChanges || hasUiChanges;
        if (
            dirty &&
            !window.confirm(
                "Switching tabs discards unsaved edits in both editors. Continue?",
            )
        ) {
            return;
        }

        resetDrafts();
        activeTab = nextTab;
    }

    function switchUiSection(nextTab: string) {
        if (nextTab !== "app" && nextTab !== "global" && nextTab !== "profiles") {
            return;
        }

        activeUiSection = nextTab;
    }

    function updateUiValue(path: Array<string | number>, nextValue: unknown) {
        uiValue = setAtPath(uiValue, path, nextValue);
        saveError = null;
    }

    function deleteUiValue(path: Array<string | number>) {
        uiValue = removeAtPath(uiValue, path);
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
    <div class="space-y-2">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div class="space-y-1 sm:flex-1">
                <div class="flex items-center gap-2">
                    <SettingsIcon class="h-4 w-4 text-slate-300" />
                    <h2 class="text-lg font-semibold text-slate-100">Configuration</h2>
                </div>
                <p class="text-xs text-slate-400">Edit your AniBridge configuration.</p>
            </div>
            <div class="flex flex-wrap gap-2 text-xs">
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-slate-100 transition-colors hover:bg-slate-800/60"
                    onclick={loadConfig}
                    disabled={loading || saving || restarting}>
                    <RefreshCw class="h-3.5 w-3.5" /> Reload
                </button>
                <a
                    href="https://anibridge.eliasbenb.dev"
                    target="_blank"
                    rel="noreferrer"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-slate-100 transition-colors hover:bg-slate-800/60">
                    <Info class="h-3.5 w-3.5" /> Docs
                </a>
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-slate-100 transition-colors hover:bg-slate-800/60 disabled:opacity-50"
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
                    class="inline-flex items-center gap-1 rounded-md border border-amber-500 bg-amber-600 px-4 py-1.5 font-semibold text-white transition-colors hover:bg-amber-500 disabled:opacity-50"
                    onclick={restartServer}
                    disabled={loading || saving || restarting}>
                    <RefreshCw
                        class={`h-3.5 w-3.5 ${restarting ? "animate-spin" : ""}`} />
                    {restarting ? "Restarting…" : "Restart Server"}
                </button>
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-blue-500 bg-blue-600 px-4 py-1.5 font-semibold text-white transition-colors hover:bg-blue-500 disabled:opacity-50"
                    onclick={handleSave}
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
    </div>

    {#if loadError}
        <div
            class="flex items-center gap-2 rounded-md border border-rose-900/60 bg-rose-950/60 px-3 py-2 text-xs text-rose-100">
            <TriangleAlert class="h-3.5 w-3.5" /> Failed to load configuration: {loadError}
        </div>
    {/if}

    {#if configAccessBlocked}
        <div
            class="rounded-md border border-amber-900/70 bg-amber-950/50 px-3 py-2 text-xs text-amber-100">
            <p class="font-medium">Configuration editor is blocked.</p>
            <p class="mt-1 text-amber-200/90">
                Configure <code class="rounded-md bg-amber-900/40 px-1"
                    >web.basic_auth</code>
                or explicitly set
                <code class="rounded-md bg-amber-900/40 px-1"
                    >web.allow_config_without_auth: true</code>
                to allow unauthenticated access.
            </p>
        </div>
    {/if}

    {#if saveError}
        <div
            class="flex items-center gap-2 rounded-md border border-rose-900/60 bg-rose-950/60 px-3 py-2 text-xs text-rose-100">
            <TriangleAlert class="h-3.5 w-3.5" />
            {saveError}
        </div>
    {/if}

    {#if restarting || restartNotice}
        <div
            class="flex items-center gap-2 rounded-md border border-amber-900/70 bg-amber-950/50 px-3 py-2 text-xs text-amber-100">
            <LoaderCircle class={`h-3.5 w-3.5 ${restarting ? "animate-spin" : ""}`} />
            {restartNotice ?? "Restarting AniBridge..."}
        </div>
    {/if}

    <div class="space-y-2">
        <Tabs.Root
            value={activeTab}
            onValueChange={switchTab}
            class="space-y-4">
            <Tabs.List
                class="inline-flex items-center gap-1 rounded-md border border-slate-800/70 bg-slate-950/50 p-1">
                <Tabs.Trigger
                    value="ui"
                    class="inline-flex h-9 items-center gap-1 rounded-md px-4 text-xs font-medium text-slate-400 transition-colors hover:text-slate-200 data-[state=active]:bg-slate-800/80 data-[state=active]:text-slate-100">
                    <SettingsIcon class="h-4 w-4" /> UI
                </Tabs.Trigger>
                <Tabs.Trigger
                    value="yaml"
                    class="inline-flex h-9 items-center gap-1 rounded-md px-4 text-xs font-medium text-slate-400 transition-colors hover:text-slate-200 data-[state=active]:bg-slate-800/80 data-[state=active]:text-slate-100">
                    <FileCodeCorner class="h-4 w-4" /> YAML
                </Tabs.Trigger>
            </Tabs.List>
        </Tabs.Root>

        <div
            class="rounded-md border border-sky-900/70 bg-sky-950/30 p-4 text-xs text-sky-100">
            <div class="flex items-start gap-3">
                <Info class="mt-0.5 h-4 w-4 shrink-0 text-sky-300" />
                <div class="min-w-0 space-y-1.5">
                    <p>
                        <span class="font-semibold text-sky-100"
                            >Configuration file:</span>
                        <code
                            class="ml-2 rounded-md bg-sky-950/60 px-1.5 py-0.5 text-[11px] text-sky-50">
                            {configPath || "(not set)"}
                        </code>
                    </p>
                    <p class="text-sky-200/80">
                        {#if fileExists}
                            The existing file will be overwritten when you save.
                        {:else}
                            A new configuration file will be created when you save.
                        {/if}
                    </p>
                    <p class="text-[11px] text-sky-200/80">
                        {#if uiSettingsError}
                            Guided UI is unavailable until the YAML parses cleanly.
                        {:else if !configSchema}
                            Guided UI availability is still loading.
                        {/if}
                        {#if hasChanges}
                            <span
                                class={uiSettingsError || !configSchema
                                    ? "ml-2 text-blue-200"
                                    : "text-blue-200"}>
                                Unsaved {activeTab === "yaml" ? "YAML" : "UI"}
                                changes.
                            </span>
                        {/if}
                    </p>
                </div>
            </div>
        </div>

        {#if activeTab === "ui"}
            {#if uiSettingsError}
                <div
                    class="flex items-center gap-2 rounded-md border border-amber-900/70 bg-amber-950/50 px-3 py-2 text-xs text-amber-100">
                    <TriangleAlert class="h-3.5 w-3.5" />
                    Guided UI is unavailable until the YAML parses cleanly: {uiSettingsError}
                </div>
            {:else if loading}
                <div
                    class="flex items-center justify-center gap-2 rounded-md border border-slate-800 bg-slate-950/70 py-32 text-xs text-slate-400 shadow-inner">
                    <LoaderCircle class="h-4 w-4 animate-spin" /> Loading configuration…
                </div>
            {:else if configSchema}
                <div
                    class="space-y-4 rounded-md border border-slate-800/80 bg-slate-950/60 p-4 shadow-sm">
                    <Tabs.Root
                        value={activeUiSection}
                        onValueChange={switchUiSection}
                        class="space-y-4">
                        <Tabs.List class="grid gap-2 sm:grid-cols-3">
                            <Tabs.Trigger
                                value="app"
                                class="flex rounded-md border border-slate-800/70 bg-slate-950/40 p-3 text-left transition-colors hover:border-slate-700/80 hover:bg-slate-900/70 data-[state=active]:border-blue-500/40 data-[state=active]:bg-slate-900 data-[state=active]:shadow-sm">
                                <div class="space-y-1">
                                    <div class="text-xs font-semibold text-slate-100">
                                        App Settings
                                    </div>
                                    <p
                                        class="text-[11px] leading-relaxed text-slate-400">
                                        App-wide settings.
                                    </p>
                                </div>
                            </Tabs.Trigger>
                            <Tabs.Trigger
                                value="global"
                                class="flex rounded-md border border-slate-800/70 bg-slate-950/40 p-3 text-left transition-colors hover:border-slate-700/80 hover:bg-slate-900/70 data-[state=active]:border-blue-500/40 data-[state=active]:bg-slate-900 data-[state=active]:shadow-sm">
                                <div class="space-y-1">
                                    <div class="text-xs font-semibold text-slate-100">
                                        Global Settings
                                    </div>
                                    <p
                                        class="text-[11px] leading-relaxed text-slate-400">
                                        Shared defaults inherited by every configured
                                        profile.
                                    </p>
                                </div>
                            </Tabs.Trigger>
                            <Tabs.Trigger
                                value="profiles"
                                class="flex rounded-md border border-slate-800/70 bg-slate-950/40 p-3 text-left transition-colors hover:border-slate-700/80 hover:bg-slate-900/70 data-[state=active]:border-blue-500/40 data-[state=active]:bg-slate-900 data-[state=active]:shadow-sm">
                                <div class="space-y-1">
                                    <div class="text-xs font-semibold text-slate-100">
                                        Profile Settings
                                    </div>
                                    <p
                                        class="text-[11px] leading-relaxed text-slate-400">
                                        Provider connections and per-profile sync
                                        behavior.
                                    </p>
                                </div>
                            </Tabs.Trigger>
                        </Tabs.List>
                    </Tabs.Root>

                    {#if activeUiSection === "app"}
                        <section
                            class="space-y-4 rounded-md border border-slate-800/70 bg-slate-950/40 p-4">
                            {#if appSettingEntries.length === 0}
                                <div
                                    class="rounded-md border border-slate-800/70 bg-slate-950/50 px-3 py-4 text-xs text-slate-400">
                                    No app-level settings are available in the current
                                    schema.
                                </div>
                            {:else}
                                <div class="space-y-4">
                                    {#each appSettingEntries as [fieldName, fieldSchema] (fieldName)}
                                        <SchemaFormNode
                                            rootSchema={configSchema}
                                            schema={fieldSchema as Record<
                                                string,
                                                unknown
                                            >}
                                            value={uiValue[fieldName]}
                                            path={[fieldName]}
                                            label={humanizeKey(fieldName)}
                                            onChange={updateUiValue}
                                            onDelete={deleteUiValue} />
                                    {/each}
                                </div>
                            {/if}
                        </section>
                    {:else if activeUiSection === "global"}
                        <section
                            class="space-y-4 rounded-md border border-slate-800/70 bg-slate-950/40 p-4">
                            {#if globalSettingsSchema}
                                <SchemaFormNode
                                    rootSchema={configSchema}
                                    schema={globalSettingsSchema}
                                    value={uiValue.global_config}
                                    path={["global_config"]}
                                    label="Global Settings"
                                    onChange={updateUiValue}
                                    onDelete={deleteUiValue} />
                            {:else}
                                <div
                                    class="rounded-md border border-slate-800/70 bg-slate-950/50 px-3 py-4 text-xs text-slate-400">
                                    Global settings are not defined in the current
                                    schema.
                                </div>
                            {/if}
                        </section>
                    {:else}
                        <section
                            class="space-y-4 rounded-md border border-slate-800/70 bg-slate-950/40 p-4">
                            {#if profilesSchema}
                                <SchemaFormNode
                                    rootSchema={configSchema}
                                    schema={profilesSchema}
                                    value={uiValue.profiles}
                                    path={["profiles"]}
                                    label="Profiles"
                                    addLabel="profile"
                                    onChange={updateUiValue}
                                    onDelete={deleteUiValue} />
                            {:else}
                                <div
                                    class="rounded-md border border-slate-800/70 bg-slate-950/50 px-3 py-4 text-xs text-slate-400">
                                    Profile settings are not defined in the current
                                    schema.
                                </div>
                            {/if}
                        </section>
                    {/if}
                </div>
            {/if}
        {:else}
            <div
                class="rounded-md border border-slate-800 bg-slate-950/70 p-2 shadow-inner">
                {#if loading}
                    <div
                        class="flex items-center justify-center gap-2 py-32 text-xs text-slate-400">
                        <LoaderCircle class="h-4 w-4 animate-spin" /> Loading configuration…
                    </div>
                {:else}
                    <div
                        class="h-[60vh] overflow-hidden rounded-md border border-slate-900/80">
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
        {/if}

        {#if hasChanges}
            <p class="text-[11px] text-slate-400">
                Unsaved changes detected in the {activeTab === "yaml" ? "YAML" : "UI"} editor.
            </p>
        {/if}
    </div>

    <div class="space-y-2">
        <h4
            class="flex items-center gap-2 text-sm font-medium tracking-wide text-slate-200">
            <Languages class="inline h-4 w-4 text-slate-400" /> AniList Title Language
        </h4>
        <p class="text-[11px] leading-relaxed text-slate-500">
            Choose which title language to prefer on the mappings page. Stored only in
            this browser.
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
