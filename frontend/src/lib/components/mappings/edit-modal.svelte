<script lang="ts">
    import { Info, Plus, RefreshCcw, Save, Trash2 } from "@lucide/svelte";
    import { Tooltip } from "bits-ui";

    import type {
        Mapping,
        MappingDetail,
        MappingOverridePayload,
        MappingTarget,
        TargetPayload,
    } from "$lib/types/api";
    import Modal from "$lib/ui/modal.svelte";
    import { apiFetch } from "$lib/utils/api";

    interface Props {
        open: boolean;
        mode?: "create" | "edit";
        mapping?: Mapping | null;
        onSaved?: (detail: MappingDetail) => void;
    }

    let {
        open = $bindable(false),
        mode = "create",
        mapping = null,
        onSaved,
    }: Props = $props();

    type RangeMode = "inherited" | "custom";

    type EditableRange = {
        source_range: string;
        upstream_value: string | null;
        custom_value: string | null;
        mode: RangeMode;
        original_source_range: string;
    };

    type EditableEntry = {
        key: string;
        authority: string;
        value: string;
        scope: string;
        authority_placeholder: string;
        value_placeholder: string;
        scope_placeholder: string;
        origin: "upstream" | "custom" | "mixed" | "deleted";
        deleted: boolean;
        original_descriptor: string;
        ranges: EditableRange[];
        removed_ranges: EditableRange[];
    };

    const DESCRIPTOR_RE = /^[^:\s]+:[^:\s]+(?::[^:\s]+)?$/;

    const fieldInfo = {
        authority:
            "Authority that the override belongs to (e.g. 'anilist', 'tvdb_show', 'tmdb_movie').",
        value: "Descriptor value from the authority that the override targets.",
        scope: "Optional scope to apply the override for (e.g. 's1').",
    } as const;

    const rangeFieldInfo = {
        source: "Source number range to match (e.g. '1', '1-12', '1-').",
        destination:
            "Destination number or range to map to (e.g. '1', '1-12', '1-6|2').",
    };

    type DescriptorState = { authority: string; value: string; scope: string };

    type DescriptorField = {
        field: keyof DescriptorState;
        placeholder: string;
        aria: string;
        info: string;
    };

    const descriptorInputs: DescriptorField[] = [
        {
            field: "authority",
            placeholder: "Authority",
            aria: "Descriptor authority",
            info: fieldInfo.authority,
        },
        {
            field: "value",
            placeholder: "Value",
            aria: "Descriptor value",
            info: fieldInfo.value,
        },
        {
            field: "scope",
            placeholder: "Scope (optional)",
            aria: "Descriptor scope",
            info: fieldInfo.scope,
        },
    ];

    const EMPTY_DESCRIPTOR: DescriptorState = { authority: "", value: "", scope: "" };

    type EntryFieldKey = "authority" | "value" | "scope";
    type EntryPlaceholderKey =
        | "authority_placeholder"
        | "value_placeholder"
        | "scope_placeholder";

    type EntryField = {
        key: EntryFieldKey;
        placeholder: EntryPlaceholderKey;
        aria: string;
        info: string;
        defaultLabel: string;
    };

    const entryFields: EntryField[] = [
        {
            key: "authority",
            placeholder: "authority_placeholder",
            aria: "Target authority",
            info: fieldInfo.authority,
            defaultLabel: "Authority",
        },
        {
            key: "value",
            placeholder: "value_placeholder",
            aria: "Target value",
            info: fieldInfo.value,
            defaultLabel: "Value",
        },
        {
            key: "scope",
            placeholder: "scope_placeholder",
            aria: "Target scope",
            info: fieldInfo.scope,
            defaultLabel: "Scope (optional)",
        },
    ];

    let descriptor = $state<DescriptorState>({ ...EMPTY_DESCRIPTOR });
    let entries = $state<EditableEntry[]>([]);
    let loadingDetail = $state(false);
    let saving = $state(false);
    let error = $state<string | null>(null);

    function buildDescriptor(
        authority: string,
        value: string,
        scope?: string | null,
    ): string {
        const base = `${authority}:${value}`;
        const cleanedScope = (scope ?? "").trim();
        return cleanedScope ? `${base}:${cleanedScope}` : base;
    }

    function descriptorFromState(): string {
        const authority = descriptor.authority.trim();
        const value = descriptor.value.trim();
        const scope = descriptor.scope.trim();
        if (!authority && !value && !scope) {
            return "";
        }
        if (!authority || !value) {
            return "";
        }
        return buildDescriptor(authority, value, scope);
    }

    function setDescriptorFields(
        authority: string,
        value: string,
        scope: string | null,
    ) {
        descriptor = { authority, value, scope: scope ?? "" };
    }

    function canLoadDescriptor(): boolean {
        const descriptor = descriptorFromState();
        return !!descriptor && DESCRIPTOR_RE.test(descriptor);
    }

    function toEditableEntries(data: MappingDetail): EditableEntry[] {
        return (data.targets || []).map((entry: MappingTarget) => {
            const isCustomEntry = entry.origin !== "upstream";
            return {
                key: entry.descriptor,
                authority: isCustomEntry ? entry.authority : "",
                value: isCustomEntry ? entry.value : "",
                scope: isCustomEntry ? (entry.scope ?? "") : "",
                authority_placeholder: entry.authority,
                value_placeholder: entry.value,
                scope_placeholder: entry.scope ?? "",
                origin: entry.origin,
                deleted: entry.deleted ?? false,
                original_descriptor: entry.descriptor,
                removed_ranges: [],
                ranges: (entry.ranges || []).map((range) => ({
                    source_range: range.source_range,
                    upstream_value: range.upstream ?? null,
                    custom_value:
                        range.origin === "custom" ? (range.effective ?? null) : null,
                    mode: (range.origin === "custom"
                        ? "custom"
                        : "inherited") as RangeMode,
                    original_source_range: range.source_range,
                })),
            };
        });
    }

    function hydrateDetail(data: MappingDetail) {
        entries = toEditableEntries(data);
        setDescriptorFields(data.authority, data.value, data.scope ?? "");
    }

    function makeKey() {
        return `tmp-${Math.random().toString(36).slice(2)}`;
    }

    function addEntry() {
        entries = [
            ...entries,
            {
                key: makeKey(),
                authority: "",
                value: "",
                scope: "",
                authority_placeholder: "",
                value_placeholder: "",
                scope_placeholder: "",
                origin: "custom",
                deleted: false,
                original_descriptor: "",
                removed_ranges: [],
                ranges: [
                    {
                        source_range: "",
                        upstream_value: null,
                        custom_value: null,
                        mode: "custom",
                        original_source_range: "",
                    },
                ],
            },
        ];
    }

    function removeEntry(key: string) {
        entries = entries
            .map((entry) => {
                if (entry.key !== key) return entry;
                // If this is a locally-created target, drop it entirely.
                if (entry.origin === "custom") {
                    return null;
                }
                return { ...entry, deleted: true, origin: "deleted" };
            })
            .filter((entry): entry is EditableEntry => entry !== null);
    }

    function updateEntry(
        key: string,
        updater: (entry: EditableEntry) => EditableEntry,
    ) {
        entries = entries.map((entry) => (entry.key === key ? updater(entry) : entry));
    }

    function addRange(key: string) {
        updateEntry(key, (entry) => ({
            ...entry,
            ranges: [
                ...entry.ranges,
                {
                    source_range: "",
                    upstream_value: null,
                    custom_value: null,
                    mode: "custom",
                    original_source_range: "",
                },
            ],
        }));
    }

    function removeRange(key: string, index: number) {
        updateEntry(key, (entry) => {
            const removed = entry.ranges[index];
            const removedSource = removed?.original_source_range.trim() || "";
            const nextRemoved = removedSource
                ? [
                      ...entry.removed_ranges.filter(
                          (r) => r.original_source_range !== removedSource,
                      ),
                      {
                          ...removed,
                          source_range: removedSource,
                          original_source_range: removedSource,
                      },
                  ]
                : entry.removed_ranges;
            const nextRanges = entry.ranges.filter((_, i) => i !== index);
            return { ...entry, ranges: nextRanges, removed_ranges: nextRemoved };
        });
    }

    function setSourceRange(key: string, index: number, value: string) {
        updateEntry(key, (entry) => ({
            ...entry,
            ranges: entry.ranges.map((r, i) =>
                i === index
                    ? {
                          ...r,
                          source_range: value,
                          mode: "custom" as RangeMode,
                          // When switching from inherited to custom via source edit,
                          // preserve the upstream destination so we don't accidentally
                          // create a null override.
                          custom_value:
                              r.mode === "inherited"
                                  ? (r.upstream_value ?? r.custom_value)
                                  : r.custom_value,
                      }
                    : r,
            ),
        }));
    }

    function setRangeValue(key: string, index: number, value: string) {
        const normalized = value.trim();
        updateEntry(key, (entry) => ({
            ...entry,
            ranges: entry.ranges.map((r, i) => {
                if (i !== index) return r;
                // Clearing the destination field when an upstream value exists
                // should revert to inherited mode (use upstream) rather than
                // creating a null override
                if (normalized === "" && r.upstream_value) {
                    return { ...r, mode: "inherited" as RangeMode, custom_value: null };
                }
                return {
                    ...r,
                    mode: "custom" as RangeMode,
                    custom_value: normalized === "" ? null : normalized,
                };
            }),
        }));
    }

    function revertEntry(key: string) {
        updateEntry(key, (entry) => {
            const seen: string[] = [];
            const ranges: EditableRange[] = [];
            for (const r of [...entry.ranges, ...entry.removed_ranges]) {
                const source = r.original_source_range.trim() || r.source_range.trim();
                if (!source || seen.includes(source)) {
                    continue;
                }
                seen.push(source);
                // Only keep ranges that have an upstream value. Custom-only
                // ranges (no upstream counterpart) should be dropped entirely
                // on revert so they don't create phantom null overrides in
                // the payload.
                if (!r.upstream_value) {
                    continue;
                }
                ranges.push({
                    ...r,
                    source_range: source,
                    mode: "inherited" as RangeMode,
                    custom_value: null,
                    original_source_range: source,
                });
            }
            return {
                ...entry,
                authority: "",
                value: "",
                scope: "",
                deleted: false,
                origin: entry.origin === "deleted" ? "upstream" : entry.origin,
                original_descriptor: entry.original_descriptor,
                ranges,
                removed_ranges: [],
            };
        });
    }

    function buildPayload(): MappingOverridePayload | null {
        const descriptor = descriptorFromState();
        if (!descriptor || !DESCRIPTOR_RE.test(descriptor)) {
            error = "Descriptor must be authority:entry[:scope]";
            return null;
        }

        const payloadTargets: TargetPayload[] = [];

        for (const entry of entries) {
            const authority = (
                entry.authority.trim() || entry.authority_placeholder
            ).trim();
            const value = (entry.value.trim() || entry.value_placeholder).trim();
            const scope = (entry.scope.trim() || entry.scope_placeholder).trim();
            if (!authority || !value) continue;

            const originalAuthority =
                entry.authority_placeholder.trim() || entry.authority.trim();
            const originalValue = entry.value_placeholder.trim() || entry.value.trim();
            const originalScope = entry.scope_placeholder.trim() || entry.scope.trim();
            const originalDescriptor =
                entry.original_descriptor ||
                (originalAuthority && originalValue
                    ? buildDescriptor(originalAuthority, originalValue, originalScope)
                    : "");
            const newDescriptor = buildDescriptor(authority, value, scope);
            const descriptorChanged =
                newDescriptor !== originalDescriptor && !!originalDescriptor;

            const removedSources: Record<string, boolean> = {};
            for (const removed of entry.removed_ranges) {
                const source = removed.original_source_range.trim();
                if (source) {
                    removedSources[source] = true;
                }
            }
            const ranges = [] as {
                source_range: string;
                destination_range: string | null;
            }[];

            if (entry.deleted) {
                payloadTargets.push({
                    authority: originalAuthority || authority,
                    value: originalValue || value,
                    scope: originalScope || scope || null,
                    ranges: [],
                    deleted: true,
                });
                continue;
            }

            for (const range of entry.ranges) {
                const source = range.source_range.trim();
                const destFromCustom = range.custom_value;
                const destFromUpstream = range.upstream_value;
                const destination_range =
                    descriptorChanged && destFromCustom == null
                        ? destFromUpstream
                        : destFromCustom;

                if (!source) {
                    const original = range.original_source_range.trim();
                    if (original) {
                        removedSources[original] = true;
                    }
                    continue;
                }

                if (removedSources[source]) {
                    delete removedSources[source];
                }

                if (descriptorChanged) {
                    // While moving descriptors, skip null destinations because they
                    // represent non-existent mappings.
                    if (destination_range === null) {
                        continue;
                    }
                    ranges.push({ source_range: source, destination_range });
                } else if (range.mode !== "inherited") {
                    ranges.push({ source_range: source, destination_range });
                }

                const original = range.original_source_range.trim();
                if (original && original !== source) {
                    removedSources[original] = true;
                }
            }

            if (!descriptorChanged) {
                for (const source_range of Object.keys(removedSources)) {
                    ranges.push({ source_range, destination_range: null });
                }
            }

            if (descriptorChanged) {
                payloadTargets.push({
                    authority: originalAuthority,
                    value: originalValue,
                    scope: originalScope || null,
                    ranges: [],
                    deleted: true,
                });
            }

            if (ranges.length === 0) continue;
            payloadTargets.push({
                authority,
                value,
                scope: scope || null,
                ranges,
                deleted: false,
            });
        }

        return { descriptor, targets: payloadTargets };
    }

    async function loadDetail(explicit?: string) {
        const descriptor = explicit?.trim() || descriptorFromState();
        if (!descriptor) {
            error = "Descriptor is required";
            return;
        }
        loadingDetail = true;
        error = null;
        try {
            const res = await apiFetch(
                `/api/mappings/${encodeURIComponent(descriptor)}`,
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = (await res.json()) as MappingDetail;
            hydrateDetail(data);
        } catch (err) {
            console.error(err);
            error = "Failed to load mapping";
            entries = [];
        } finally {
            loadingDetail = false;
        }
    }

    async function handleSave() {
        const payload = buildPayload();
        if (!payload) return;
        saving = true;
        error = null;
        try {
            const method = mode === "edit" ? "PUT" : "POST";
            const url =
                mode === "edit"
                    ? `/api/mappings/${encodeURIComponent(payload.descriptor)}`
                    : "/api/mappings";
            const res = await apiFetch(
                url,
                {
                    method,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                },
                {
                    successMessage:
                        mode === "edit" ? "Override updated" : "Override created",
                },
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = (await res.json()) as MappingDetail;
            hydrateDetail(data);
            onSaved?.(data);
            open = false;
        } catch (err) {
            console.error(err);
            error = "Failed to save override";
        } finally {
            saving = false;
        }
    }

    $effect(() => {
        if (!open) {
            descriptor = { ...EMPTY_DESCRIPTOR };
            entries = [];
            loadingDetail = false;
            saving = false;
            error = null;
            return;
        }

        const nextDescriptor = mode === "edit" ? (mapping?.descriptor ?? "") : "";
        descriptor =
            mode === "edit"
                ? {
                      authority: mapping?.authority ?? "",
                      value: mapping?.value ?? "",
                      scope: mapping?.scope ?? "",
                  }
                : { ...EMPTY_DESCRIPTOR };
        entries = [];
        if (mode === "edit" && nextDescriptor) {
            loadDetail(nextDescriptor);
        }
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
            {mode === "edit" ? "Edit Mapping Override" : "Create Mapping Override"}
        </div>
    {/snippet}

    <div class="rounded-md border border-slate-800/70 bg-slate-900/70 p-4 shadow-inner">
        <div class="flex flex-wrap items-start justify-between gap-4">
            <div class="flex-1 space-y-3">
                <div class="grid gap-3 sm:grid-cols-3">
                    {#each descriptorInputs as field (field.field)}
                        <div class="relative w-full">
                            <input
                                class="h-9 w-full rounded-md border border-slate-700/60 bg-slate-950 px-3 pr-10 text-[12px] text-slate-100 placeholder:text-slate-500 focus:border-emerald-500 focus:outline-none"
                                placeholder={field.placeholder}
                                aria-label={field.aria}
                                bind:value={descriptor[field.field]}
                                disabled={mode === "edit"} />
                            <div class="absolute inset-y-0 right-2 flex items-center">
                                <Tooltip.Root delayDuration={120}>
                                    <Tooltip.Trigger>
                                        <button
                                            type="button"
                                            class="pointer-events-auto inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700/60 bg-slate-900/70 text-slate-400 transition-colors hover:text-slate-100 focus-visible:ring-2 focus-visible:ring-emerald-500/40 focus-visible:outline-none"
                                            aria-label={`${field.placeholder} info`}>
                                            <Info class="h-3 w-3" />
                                        </button>
                                    </Tooltip.Trigger>
                                    <Tooltip.Portal>
                                        <Tooltip.Content
                                            side="top"
                                            sideOffset={6}
                                            class="z-50 rounded-md border border-slate-800/70 bg-slate-900/95 px-2 py-1.5 text-[11px] text-slate-100 shadow-xl">
                                            {field.info}
                                            <Tooltip.Arrow />
                                        </Tooltip.Content>
                                    </Tooltip.Portal>
                                </Tooltip.Root>
                            </div>
                        </div>
                    {/each}
                </div>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-[11px]">
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700/60 bg-slate-800/60 px-3 py-2 text-xs font-semibold text-slate-100 shadow-sm transition-colors hover:border-emerald-500 hover:text-emerald-100 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none"
                    onclick={() => loadDetail()}
                    disabled={loadingDetail || !canLoadDescriptor()}>
                    <RefreshCcw class="inline h-3.5 w-3.5" />
                    Load existing
                </button>
                <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border border-slate-700/60 bg-slate-800/60 px-3 py-2 text-xs font-semibold text-slate-100 shadow-sm transition-colors hover:border-emerald-500 hover:text-emerald-100 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none"
                    onclick={addEntry}
                    disabled={saving}>
                    <Plus class="inline h-3.5 w-3.5" />
                    Add Mapping
                </button>
            </div>
        </div>
    </div>

    {#if entries.length === 0}
        <div
            class="rounded-md border border-dashed border-slate-800 bg-slate-950/70 p-6 text-center text-sm text-slate-400">
            Load an existing mapping or add a mapping to start.
        </div>
    {:else}
        <div class="space-y-4">
            {#each entries as entry (entry.key)}
                <div
                    class={`rounded-md border p-4 shadow-md ${
                        entry.deleted && entry.origin !== "custom"
                            ? "border-rose-900/60 bg-slate-950/50 opacity-70"
                            : "border-slate-800 bg-slate-950/80"
                    }`}>
                    <div class="flex flex-wrap items-center gap-1.5">
                        <span
                            class={`font-mono text-xs ${
                                entry.deleted && entry.origin !== "custom"
                                    ? "text-slate-500 line-through"
                                    : "text-slate-300"
                            }`}>
                            {buildDescriptor(
                                entry.authority || entry.authority_placeholder || "?",
                                entry.value || entry.value_placeholder || "?",
                                entry.scope || entry.scope_placeholder || "",
                            )}
                        </span>
                        <span
                            class={`rounded border px-2 py-0.5 text-[10px] font-semibold tracking-wide uppercase ${
                                entry.origin === "custom"
                                    ? "border-emerald-800/60 bg-emerald-900/40 text-emerald-100"
                                    : entry.origin === "mixed"
                                      ? "border-amber-800/60 bg-amber-900/40 text-amber-100"
                                      : "border-slate-700/70 bg-slate-800/60 text-slate-100"
                            }`}>
                            {entry.origin === "custom"
                                ? "Custom"
                                : entry.origin === "mixed"
                                  ? "Mixed"
                                  : "Upstream"}
                        </span>
                        {#if entry.deleted && entry.origin !== "custom"}
                            <span
                                class="rounded border border-rose-800/60 bg-rose-900/40 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-rose-100 uppercase">
                                Disabled override
                            </span>
                        {/if}
                        <span class="flex-1"></span>
                        {#if entry.origin !== "custom"}
                            <button
                                type="button"
                                class="inline-flex items-center gap-1 rounded-md border border-slate-700/60 bg-slate-900/60 px-3 py-1 text-[12px] font-semibold text-slate-200 transition-colors hover:border-slate-500 focus:outline-none"
                                onclick={() => revertEntry(entry.key)}>
                                Revert to upstream
                            </button>
                        {/if}
                        <button
                            type="button"
                            class="inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-emerald-200 shadow-sm transition-colors hover:border-emerald-400 hover:text-emerald-100 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-emerald-200"
                            title="Add range"
                            onclick={() => addRange(entry.key)}
                            disabled={entry.deleted}>
                            <Plus class="inline h-4 w-4" />
                        </button>
                        <button
                            type="button"
                            class="inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-rose-200 transition-colors hover:border-rose-500 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-rose-200"
                            title="Remove target mapping"
                            onclick={() => removeEntry(entry.key)}
                            disabled={entry.deleted}>
                            <Trash2 class="inline h-4 w-4" />
                        </button>
                    </div>

                    <div class="mt-3 grid gap-3 sm:grid-cols-3">
                        {#each entryFields as field (field.key)}
                            <div class="relative w-full">
                                <input
                                    class="h-9 w-full rounded-md border border-slate-800/70 bg-slate-900 px-3 pr-10 text-[13px] text-slate-100 placeholder:text-slate-500 placeholder:opacity-70 focus:border-emerald-500 focus:outline-none"
                                    placeholder={entry[field.placeholder] ||
                                        field.defaultLabel}
                                    aria-label={field.aria}
                                    bind:value={entry[field.key]}
                                    disabled={entry.deleted} />
                                <div
                                    class="absolute inset-y-0 right-2 flex items-center">
                                    <Tooltip.Root delayDuration={120}>
                                        <Tooltip.Trigger>
                                            <button
                                                type="button"
                                                class="pointer-events-auto inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700/60 bg-slate-900/70 text-slate-400 transition-colors hover:text-slate-100 focus-visible:ring-2 focus-visible:ring-emerald-500/40 focus-visible:outline-none"
                                                aria-label={`${field.defaultLabel} info`}>
                                                <Info class="h-3 w-3" />
                                            </button>
                                        </Tooltip.Trigger>
                                        <Tooltip.Portal>
                                            <Tooltip.Content
                                                side="top"
                                                sideOffset={6}
                                                class="z-50 rounded-md border border-slate-800/70 bg-slate-900/95 px-2 py-1.5 text-[11px] text-slate-100 shadow-xl">
                                                {field.info}
                                                <Tooltip.Arrow />
                                            </Tooltip.Content>
                                        </Tooltip.Portal>
                                    </Tooltip.Root>
                                </div>
                            </div>
                        {/each}
                    </div>

                    <div class="mt-3 space-y-2 border-l border-slate-800/60 pl-4">
                        {#each entry.ranges as range, idx (idx)}
                            <div
                                class={`flex flex-wrap items-center gap-2 ${
                                    range.mode === "custom" &&
                                    range.custom_value === null
                                        ? "opacity-70"
                                        : ""
                                }`}>
                                <div class="relative w-full min-w-44 flex-1">
                                    <input
                                        class={`w-full min-w-0 rounded-md border border-slate-800/70 bg-slate-900 px-3 py-1 pr-10 text-[11px] placeholder:text-slate-500 placeholder:opacity-80 focus:border-emerald-500 focus:outline-none ${
                                            range.mode === "custom" &&
                                            range.custom_value === null
                                                ? "text-slate-500 line-through"
                                                : "text-slate-100"
                                        }`}
                                        value={range.mode === "custom"
                                            ? range.source_range
                                            : ""}
                                        oninput={(ev) =>
                                            setSourceRange(
                                                entry.key,
                                                idx,
                                                ev.currentTarget.value,
                                            )}
                                        placeholder={range.source_range ||
                                            "Source range"}
                                        aria-label="Range source"
                                        disabled={entry.deleted} />
                                    <div
                                        class="absolute inset-y-0 right-2 flex items-center">
                                        <Tooltip.Root delayDuration={120}>
                                            <Tooltip.Trigger>
                                                <button
                                                    type="button"
                                                    class="pointer-events-auto inline-flex h-5 w-5 items-center justify-center rounded-full border border-slate-700/60 bg-slate-900/70 text-slate-400 transition-colors hover:text-slate-100 focus-visible:ring-2 focus-visible:ring-emerald-500/40 focus-visible:outline-none"
                                                    aria-label="Range source info">
                                                    <Info class="h-3 w-3" />
                                                </button>
                                            </Tooltip.Trigger>
                                            <Tooltip.Portal>
                                                <Tooltip.Content
                                                    side="top"
                                                    sideOffset={6}
                                                    class="z-50 rounded-md border border-slate-800/70 bg-slate-900/95 px-2 py-1 text-[11px] text-slate-100 shadow-xl">
                                                    {rangeFieldInfo.source}
                                                    <Tooltip.Arrow />
                                                </Tooltip.Content>
                                            </Tooltip.Portal>
                                        </Tooltip.Root>
                                    </div>
                                </div>
                                <div class="relative w-full min-w-44 flex-1">
                                    <input
                                        class={`w-full min-w-0 rounded-md border border-slate-800/70 bg-slate-900 px-3 py-1 pr-10 text-[11px] placeholder:text-slate-500 placeholder:opacity-80 focus:border-emerald-500 focus:outline-none ${
                                            range.mode === "custom" &&
                                            range.custom_value === null
                                                ? "text-slate-500 line-through"
                                                : "text-slate-100"
                                        }`}
                                        value={range.mode === "custom"
                                            ? (range.custom_value ?? "")
                                            : ""}
                                        oninput={(ev) =>
                                            setRangeValue(
                                                entry.key,
                                                idx,
                                                ev.currentTarget.value,
                                            )}
                                        placeholder={range.mode === "custom" &&
                                        range.custom_value === null
                                            ? "null (mapping removed)"
                                            : (range.upstream_value ??
                                              "Destination range")}
                                        aria-label="Range destination"
                                        disabled={entry.deleted} />
                                    <div
                                        class="absolute inset-y-0 right-2 flex items-center">
                                        <Tooltip.Root delayDuration={120}>
                                            <Tooltip.Trigger>
                                                <button
                                                    type="button"
                                                    class="pointer-events-auto inline-flex h-5 w-5 items-center justify-center rounded-full border border-slate-700/60 bg-slate-900/70 text-slate-400 transition-colors hover:text-slate-100 focus-visible:ring-2 focus-visible:ring-emerald-500/40 focus-visible:outline-none"
                                                    aria-label="Range destination info">
                                                    <Info class="h-3 w-3" />
                                                </button>
                                            </Tooltip.Trigger>
                                            <Tooltip.Portal>
                                                <Tooltip.Content
                                                    side="top"
                                                    sideOffset={6}
                                                    class="z-50 rounded-md border border-slate-800/70 bg-slate-900/95 px-2 py-1.5 text-[11px] text-slate-100 shadow-xl">
                                                    {rangeFieldInfo.destination}
                                                    <Tooltip.Arrow />
                                                </Tooltip.Content>
                                            </Tooltip.Portal>
                                        </Tooltip.Root>
                                    </div>
                                </div>
                                <div class="flex items-center gap-1.5">
                                    {#if range.mode === "custom" && range.custom_value === null}
                                        <span
                                            class="rounded border border-rose-800/60 bg-rose-900/30 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-rose-100 uppercase"
                                            title="Explicit null: this source range is disabled for this target">
                                            Null override
                                        </span>
                                    {/if}
                                    <button
                                        type="button"
                                        class="inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-rose-200 transition-colors hover:border-rose-500 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-rose-200"
                                        title="Remove range"
                                        onclick={() => removeRange(entry.key, idx)}
                                        disabled={entry.deleted}>
                                        <Trash2 class="inline h-4 w-4" />
                                    </button>
                                </div>
                            </div>
                        {/each}
                    </div>
                </div>
            {/each}
        </div>
    {/if}

    {#if error}
        <div
            class="rounded-md border border-rose-800 bg-rose-900/40 p-3 text-[12px] text-rose-100">
            {error}
        </div>
    {/if}
    <div class="flex flex-wrap items-center gap-2">
        <button
            type="button"
            class="inline-flex items-center gap-2 rounded-md border border-emerald-600/60 bg-emerald-600/30 px-3 py-2 text-[12px] font-semibold text-emerald-100 shadow-sm transition-colors hover:bg-emerald-600/40 focus:ring-2 focus:ring-emerald-500/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            onclick={handleSave}
            disabled={saving}>
            {#if saving}
                <Save class="h-4 w-4 animate-spin" />
            {:else}
                <Save class="h-4 w-4" />
            {/if}
            Save
        </button>
        <button
            type="button"
            class="inline-flex items-center gap-2 rounded-md border border-slate-700/60 bg-slate-800/60 px-3 py-2 text-[12px] font-semibold text-slate-100 shadow-sm transition-colors hover:border-slate-500 focus:ring-2 focus:ring-slate-500/40 focus:outline-none"
            onclick={() => loadDetail()}
            disabled={loadingDetail || !canLoadDescriptor()}>
            <RefreshCcw class="h-4 w-4" />
            Reload
        </button>
    </div>
</Modal>
