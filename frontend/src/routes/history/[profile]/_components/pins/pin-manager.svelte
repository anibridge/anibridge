<script lang="ts">
    /* eslint-disable svelte/no-navigation-without-resolve */
    import {
        ExternalLink,
        LoaderCircle,
        Pin,
        PinOff,
        RefreshCcw,
        Search,
        X,
    } from "@lucide/svelte";

    import type {
        PinListResponse,
        PinResponse,
        PinSearchResponse,
        PinSearchResult,
        ProviderMediaMetadata,
        RefPayload,
    } from "$lib/types/api";
    import { apiFetch } from "$lib/utils/api";
    import { qualifiedRefLabel } from "$lib/utils/provider-ref";

    interface Props {
        profile: string;
        onClose: () => void;
        onChanged?: () => void;
    }

    let { profile, onClose, onChanged }: Props = $props();

    let pins: PinResponse[] = $state([]);
    let results: PinSearchResult[] = $state([]);
    let query = $state("");
    let loadingPins = $state(true);
    let searching = $state(false);
    let searched = $state(false);
    let actingKey: string | null = $state(null);
    let loadedProfile = $state("");

    $effect(() => {
        if (profile && loadedProfile !== profile) {
            loadedProfile = profile;
            void loadPins();
        }
    });

    function pinPath(key: string) {
        return `/api/pins/${encodeURIComponent(profile)}/${encodeURIComponent(key)}`;
    }

    function anchorRef(key: string): RefPayload {
        return { key, path: [] };
    }

    function refIdentity(ref: RefPayload): string {
        return JSON.stringify({ key: ref.key, path: ref.path ?? [] });
    }

    function pinnedForRef(ref: RefPayload): PinResponse | null {
        const identity = refIdentity(ref);
        return (
            pins.find((pin) => refIdentity(pin.target_parent_ref) === identity) ?? null
        );
    }

    async function loadPins() {
        loadingPins = true;
        try {
            const response = await apiFetch(
                `/api/pins/${encodeURIComponent(profile)}?with_media=true`,
                undefined,
                { silent: true },
            );
            if (!response.ok) return;
            const data = (await response.json()) as PinListResponse;
            pins = data.pins ?? [];
        } finally {
            loadingPins = false;
        }
    }

    async function searchPins() {
        const text = query.trim();
        if (!text) {
            results = [];
            searched = false;
            return;
        }
        searching = true;
        searched = true;
        try {
            const params = new URLSearchParams({ q: text, limit: "12" });
            const response = await apiFetch(
                `/api/pins/${encodeURIComponent(profile)}/search?${params.toString()}`,
            );
            if (!response.ok) return;
            const data = (await response.json()) as PinSearchResponse;
            results = data.results ?? [];
        } finally {
            searching = false;
        }
    }

    async function pinKey(key: string) {
        actingKey = refIdentity(anchorRef(key));
        try {
            const response = await apiFetch(
                pinPath(key),
                { method: "PUT" },
                { successMessage: "Pinned target" },
            );
            if (!response.ok) return;
            await loadPins();
            onChanged?.();
        } finally {
            actingKey = null;
        }
    }

    async function unpinRef(ref: RefPayload) {
        const identity = refIdentity(ref);
        actingKey = identity;
        try {
            const response = await apiFetch(
                pinPath(ref.key),
                {
                    method: "DELETE",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({ target_ref: ref }),
                },
                { successMessage: "Unpinned target" },
            );
            if (!response.ok) return;
            pins = pins.filter(
                (pin) => refIdentity(pin.target_parent_ref) !== identity,
            );
            onChanged?.();
        } finally {
            actingKey = null;
        }
    }

    function titleForMedia(media: ProviderMediaMetadata) {
        return media.title || media.key;
    }

    function titleFor(pin: PinResponse) {
        return (
            pin.media?.title ||
            qualifiedRefLabel(pin.target_namespace, pin.target_parent_ref) ||
            pin.target_parent_ref.key
        );
    }

    function subtitleFor(pin: PinResponse) {
        return (
            qualifiedRefLabel(pin.target_namespace, pin.target_parent_ref) ||
            pin.target_parent_ref.key
        );
    }

    function dateLabel(value: string) {
        return new Date(value).toLocaleString();
    }

    function fallbackInitial(value: string) {
        return value.trim().slice(0, 1).toUpperCase() || "?";
    }
</script>

<div
    class="fixed inset-0 z-40 bg-slate-950/65 backdrop-blur-sm"
    role="presentation"
    onclick={onClose}>
</div>
<aside
    class="fixed top-0 right-0 z-50 flex h-dvh w-full max-w-lg flex-col border-l border-slate-800 bg-slate-950 shadow-2xl sm:w-lg"
    aria-label="Pin manager">
    <header class="border-b border-slate-800 bg-slate-950/95 px-4 py-3">
        <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
                <div
                    class="flex items-center gap-2 text-sm font-semibold text-slate-100">
                    <Pin class="h-4 w-4 text-sky-300" />
                    Pins
                </div>
                <div class="mt-0.5 truncate text-[11px] text-slate-500">{profile}</div>
            </div>
            <span
                class="rounded-md border border-slate-800 bg-slate-900 px-2 py-1 text-[11px] font-medium text-slate-400">
                {pins.length} pinned
            </span>
            <button
                type="button"
                class="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-700 bg-slate-900 text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-100 focus:ring-2 focus:ring-slate-500/40 focus:outline-none"
                aria-label="Close pins"
                title="Close pins"
                onclick={onClose}>
                <X class="h-4 w-4" />
            </button>
        </div>
    </header>

    <div class="flex-1 overflow-y-auto px-4 py-4">
        <section class="rounded-md border border-slate-800 bg-slate-900/45 p-3">
            <form
                class="flex gap-2"
                onsubmit={(event) => {
                    event.preventDefault();
                    void searchPins();
                }}>
                <label
                    class="sr-only"
                    for="pin-search">Search target provider</label>
                <div class="relative min-w-0 flex-1">
                    <Search
                        class="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-500" />
                    <input
                        id="pin-search"
                        bind:value={query}
                        class="h-9 w-full rounded-md border border-slate-700 bg-slate-950 pr-3 pl-8 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-sky-600 focus:ring-2 focus:ring-sky-600/20"
                        placeholder="Search targets"
                        autocomplete="off" />
                </div>
                <button
                    type="submit"
                    class="inline-flex h-9 w-9 items-center justify-center rounded-md border border-sky-700/60 bg-sky-600/20 text-sky-100 transition-colors hover:bg-sky-600/30 disabled:cursor-wait disabled:opacity-60"
                    disabled={searching}
                    aria-label="Search targets"
                    title="Search targets">
                    {#if searching}<LoaderCircle
                            class="h-4 w-4 animate-spin" />{:else}<Search
                            class="h-4 w-4" />{/if}
                </button>
            </form>

            {#if searched}
                <div class="mt-3 border-t border-slate-800 pt-3">
                    <div class="mb-2 flex items-center justify-between gap-3">
                        <h2
                            class="text-xs font-semibold tracking-wide text-slate-400 uppercase">
                            Search results
                        </h2>
                        {#if searching}
                            <span
                                class="inline-flex items-center gap-1 text-[11px] text-sky-300">
                                <LoaderCircle class="h-3 w-3 animate-spin" /> searching
                            </span>
                        {/if}
                    </div>
                    {#if !searching && results.length === 0}
                        <div
                            class="rounded-md border border-dashed border-slate-700 bg-slate-950/50 p-4 text-center text-sm text-slate-500">
                            No targets found
                        </div>
                    {/if}
                </div>
            {/if}

            {#if results.length}
                <div class="space-y-2">
                    {#each results as result (result.media.key)}
                        {@const mediaRef = anchorRef(result.media.key)}
                        {@const pinned = result.pin ?? pinnedForRef(mediaRef)}
                        {@const isPinned = !!pinned}
                        {@const mediaIdentity = refIdentity(mediaRef)}
                        <div
                            class="flex items-center gap-3 rounded-md border border-slate-800 bg-slate-950/60 p-2 transition-colors hover:border-slate-700/80">
                            <div
                                class="relative flex h-12 w-9 shrink-0 items-center justify-center overflow-hidden rounded border border-slate-800 bg-slate-900">
                                {#if result.media.poster_url}
                                    <img
                                        src={result.media.poster_url}
                                        alt=""
                                        class="max-h-full max-w-full object-contain" />
                                {:else}
                                    <span class="text-sm font-semibold text-slate-500"
                                        >{fallbackInitial(
                                            titleForMedia(result.media),
                                        )}</span>
                                {/if}
                            </div>
                            <div class="min-w-0 flex-1">
                                <div
                                    class="truncate text-sm font-medium text-slate-100">
                                    {titleForMedia(result.media)}
                                </div>
                                <div class="truncate text-[11px] text-slate-500">
                                    {result.media.namespace}@{result.media.key}
                                </div>
                            </div>
                            <button
                                type="button"
                                class={`inline-flex h-8 w-8 items-center justify-center rounded-md border transition-colors disabled:cursor-wait disabled:opacity-60 ${isPinned ? "border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800" : "border-sky-700/60 bg-sky-600/20 text-sky-100 hover:bg-sky-600/30"}`}
                                disabled={actingKey === mediaIdentity}
                                aria-label={isPinned ? "Unpin target" : "Pin target"}
                                title={isPinned ? "Unpin target" : "Pin target"}
                                onclick={() =>
                                    void (isPinned
                                        ? unpinRef(pinned.target_parent_ref)
                                        : pinKey(result.media.key))}>
                                {#if actingKey === mediaIdentity}<LoaderCircle
                                        class="h-3.5 w-3.5 animate-spin" />{:else if isPinned}<PinOff
                                        class="h-3.5 w-3.5" />{:else}<Pin
                                        class="h-3.5 w-3.5" />{/if}
                            </button>
                        </div>
                    {/each}
                </div>
            {/if}
        </section>

        <section class="mt-4 space-y-2">
            <div class="flex items-center justify-between gap-3">
                <h2
                    class="text-xs font-semibold tracking-wide text-slate-400 uppercase">
                    Pinned targets
                </h2>
                <button
                    type="button"
                    class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-800 bg-slate-950 text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-100"
                    aria-label="Refresh pins"
                    title="Refresh pins"
                    onclick={() => void loadPins()}>
                    <RefreshCcw class="h-3.5 w-3.5" />
                </button>
            </div>

            {#if loadingPins}
                <div
                    class="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-900/50 p-3 text-sm text-slate-400">
                    <LoaderCircle class="h-4 w-4 animate-spin" /> Loading pins
                </div>
            {:else if pins.length === 0}
                <div
                    class="rounded-md border border-dashed border-slate-700 bg-slate-900/35 p-6 text-center text-sm text-slate-500">
                    No pinned targets
                </div>
            {:else}
                <div class="space-y-2">
                    {#each pins as pin (refIdentity(pin.target_parent_ref))}
                        {@const pinIdentity = refIdentity(pin.target_parent_ref)}
                        <article
                            class="flex items-center gap-3 rounded-md border border-slate-800 bg-slate-900/50 p-2.5 transition-colors hover:border-slate-700/80">
                            <div
                                class="relative flex h-14 w-10 shrink-0 items-center justify-center overflow-hidden rounded border border-slate-800 bg-slate-950">
                                {#if pin.media?.poster_url}
                                    <img
                                        src={pin.media.poster_url}
                                        alt=""
                                        class="max-h-full max-w-full object-contain" />
                                {:else}
                                    <span class="text-base font-semibold text-slate-500"
                                        >{fallbackInitial(titleFor(pin))}</span>
                                {/if}
                            </div>
                            <div class="min-w-0 flex-1">
                                <div
                                    class="truncate text-sm font-medium text-slate-100">
                                    {titleFor(pin)}
                                </div>
                                <div class="truncate text-[11px] text-slate-500">
                                    {subtitleFor(pin)}
                                </div>
                                <div
                                    class="mt-1 text-[10px] text-slate-600"
                                    title={dateLabel(pin.updated_at)}>
                                    updated {dateLabel(pin.updated_at)}
                                </div>
                            </div>
                            {#if pin.media?.external_url}
                                <a
                                    href={pin.media.external_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    data-sveltekit-reload
                                    class="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-800 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
                                    aria-label="Open target"
                                    title="Open target">
                                    <ExternalLink class="h-3.5 w-3.5" />
                                </a>
                            {/if}
                            <button
                                type="button"
                                class="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-700 bg-slate-950 text-slate-300 transition-colors hover:bg-slate-800 disabled:cursor-wait disabled:opacity-60"
                                disabled={actingKey === pinIdentity}
                                aria-label="Unpin target"
                                title="Unpin target"
                                onclick={() => void unpinRef(pin.target_parent_ref)}>
                                {#if actingKey === pinIdentity}<LoaderCircle
                                        class="h-3.5 w-3.5 animate-spin" />{:else}<PinOff
                                        class="h-3.5 w-3.5" />{/if}
                            </button>
                        </article>
                    {/each}
                </div>
            {/if}
        </section>
    </div>
</aside>
