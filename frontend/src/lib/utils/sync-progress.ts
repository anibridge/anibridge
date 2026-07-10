import type { CurrentSync } from "$lib/types/api";
import { titleCase } from "$lib/utils/text";

export function progressPercent(sync?: CurrentSync | null): number | null {
    if (!sync || sync.state !== "running") return null;
    const processed = Math.max(0, sync.processed_items ?? 0);
    if (typeof sync.total_items !== "number") return null;
    const total = Math.max(0, sync.total_items);
    if (total <= 0) return 0;
    return Math.max(0, Math.min(1, processed / total));
}

export function progressCount(sync?: CurrentSync | null): string {
    if (!sync) return "0/0";
    const processed = Math.max(0, sync.processed_items ?? 0);
    const scanned = Math.max(0, sync.scanned_items ?? 0);
    if (typeof sync.total_items === "number") {
        const total = Math.max(0, sync.total_items);
        return `${processed}/${total > 0 ? total : "?"}`;
    }
    if (scanned > processed) return `${processed} processed · ${scanned} found`;
    return `${processed} processed`;
}

export function progressSubject(sync?: CurrentSync | null): string | null {
    if (!sync) return null;
    const source = sync.source_namespace || "source";
    const target = sync.target_namespace || "target";
    return `${titleCase(source)} -> ${titleCase(target)}`;
}

export function progressStage(sync?: CurrentSync | null): string {
    if (!sync) return "Processing";
    const trigger = sync.trigger ? `${titleCase(sync.trigger)} ` : "";
    return `${trigger}${titleCase(sync.stage || sync.state || "processing")}`;
}
