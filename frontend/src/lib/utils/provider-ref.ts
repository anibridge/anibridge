import type { HistoryItem, PinResponse, RefPayload } from "$lib/types/api";

export interface ProviderIdentifier {
    namespace: string;
    key: string;
}

function refKey(ref?: RefPayload | null): string | null {
    const key = ref?.key?.trim();
    return key ? key : null;
}

export function refLabel(ref?: RefPayload | null): string | null {
    const key = refKey(ref);
    if (!key) return null;
    const path = ref?.path ?? [];
    if (!path.length) return key;
    const suffix = path.map((step) => `${step.axis}:${step.value}`).join("/");
    return `${key} ${suffix}`;
}

export function targetIdentifier(item: HistoryItem): ProviderIdentifier | null {
    const namespace = item.target_namespace ?? item.target_media?.namespace ?? null;
    const key = refKey(item.target_ref) ?? item.target_media?.key ?? null;
    if (!namespace || !key) return null;
    return { namespace, key };
}

export function pinIdentifier(pin: PinResponse): ProviderIdentifier | null {
    const namespace = pin.target_namespace ?? pin.media?.namespace ?? null;
    const key = refKey(pin.target_ref) ?? pin.media?.key ?? null;
    if (!namespace || !key) return null;
    return { namespace, key };
}
