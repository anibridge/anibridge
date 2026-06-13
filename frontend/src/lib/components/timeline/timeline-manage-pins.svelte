<script lang="ts">
    import { onDestroy, onMount } from "svelte";

    import PinFieldsEditor from "$lib/components/timeline/pin-fields-editor.svelte";
    import type { HistoryItem, PinFieldOption, PinResponse } from "$lib/types/api";
    import { apiFetch } from "$lib/utils/api";
    import { toast } from "$lib/utils/notify";
    import { loadPinOptions } from "$lib/utils/pin-options";
    import { targetIdentifier } from "$lib/utils/provider-ref";

    interface Props {
        profile: string;
        item: HistoryItem;
        onDraft?: (fields: string[]) => void;
        onSaved?: (fields: string[]) => void;
        onBusy?: (value: boolean) => void;
    }

    let { profile, item, onDraft, onSaved, onBusy }: Props = $props();

    let options: PinFieldOption[] = $state([]);
    let optionsLoading = $state(false);
    let optionsError: string | null = $state(null);
    let optionsErrorNotified = false;
    let saving = $state(false);
    let error: string | null = $state(null);
    let selected: string[] = $state([]);
    let baseline: string[] = $state([]);

    function arraysEqual(a: string[], b: string[]): boolean {
        if (a.length !== b.length) return false;
        for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
        return true;
    }

    function emitBusy(value: boolean) {
        onBusy?.(value);
    }

    function setSelection(fields: string[], updateBaseline = false) {
        selected = [...fields];
        if (updateBaseline) baseline = [...fields];
        onDraft?.([...fields]);
    }

    async function loadOptions(force = false) {
        optionsLoading = true;
        if (force) optionsErrorNotified = false;
        optionsError = null;
        try {
            const loaded = await loadPinOptions(force);
            options = [...loaded];
            optionsError = null;
        } catch (e) {
            console.error("Failed to load pin options", e);
            optionsError = (e as Error)?.message || "Failed to load pin options";
            if (!optionsErrorNotified) {
                toast("Failed to load pin options", "error");
                optionsErrorNotified = true;
            }
        } finally {
            optionsLoading = false;
        }
    }

    async function initialize() {
        emitBusy(true);
        try {
            await loadOptions(false);
        } finally {
            emitBusy(false);
        }
    }

    async function saveSelection(fields: string[] = selected) {
        if (saving) return;
        const identifier = targetIdentifier(item);
        if (!identifier) {
            toast("Pins require a target record.", "error");
            return;
        }
        if (arraysEqual(fields, baseline)) return;
        saving = true;
        error = null;
        emitBusy(true);
        const key = encodeURIComponent(identifier.key);
        try {
            if (!fields.length) {
                const res = await apiFetch(
                    `/api/pins/${profile}/${key}`,
                    { method: "DELETE" },
                    { successMessage: "Pins cleared" },
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                setSelection([], true);
                onSaved?.([]);
                return;
            }
            const res = await apiFetch(
                `/api/pins/${profile}/${key}?with_media=true`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ fields }),
                },
                { successMessage: "Pins updated" },
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = (await res.json()) as PinResponse;
            const next = data.fields ?? [];
            setSelection(next, true);
            onSaved?.([...next]);
        } catch (e) {
            console.error("Failed to save pins", e);
            error = (e as Error)?.message || "Failed to save pins";
            toast("Failed to save pins", "error");
        } finally {
            saving = false;
            emitBusy(false);
        }
    }

    async function refreshAll(force = true) {
        emitBusy(true);
        try {
            await loadOptions(force);
            error = null;
            const base = Array.isArray(item.pinned_fields) ? item.pinned_fields : [];
            setSelection(base, true);
        } finally {
            emitBusy(false);
        }
    }

    onMount(() => {
        const base = Array.isArray(item.pinned_fields) ? item.pinned_fields : [];
        setSelection(base, true);
        void initialize();
    });

    onDestroy(() => {
        emitBusy(false);
    });

    $effect(() => {
        const base = Array.isArray(item.pinned_fields) ? item.pinned_fields : [];
        if (!saving && !arraysEqual(base, baseline)) {
            setSelection(base, true);
        }
    });
</script>

<PinFieldsEditor
    bind:value={selected}
    {baseline}
    {options}
    loading={optionsLoading}
    {saving}
    {error}
    {optionsError}
    missingMessage={targetIdentifier(item) ? null : "Pins require a target record."}
    title="Pin fields"
    subtitle="Choose the fields to keep unchanged for this entry when syncing."
    disabled={!targetIdentifier(item)}
    onSave={(value) => void saveSelection(value)}
    onRefresh={(force) => void refreshAll(force)}
    onChange={(value) => setSelection(value)} />
