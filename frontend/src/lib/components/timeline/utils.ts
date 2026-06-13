import { SvelteSet } from "svelte/reactivity";

import type { DiffEntry } from "$lib/components/timeline/types";
import type { HistoryItem } from "$lib/types/api";

export function buildDiff(item: HistoryItem): DiffEntry[] {
    const before = item.before_state?.values || {};
    const after = item.after_state?.values || {};
    const paths = new SvelteSet<string>();
    const visit = (obj: unknown, base = "") => {
        if (!obj || typeof obj !== "object") return;
        for (const k of Object.keys(obj as Record<string, unknown>)) {
            const val = (obj as Record<string, unknown>)[k];
            const path = base ? `${base}.${k}` : k;
            if (val && typeof val === "object" && !Array.isArray(val)) visit(val, path);
            else paths.add(path);
        }
    };
    visit(before);
    visit(after);
    const diff: DiffEntry[] = [];
    for (const p of paths) {
        const segs = p.split(".");
        const get = (root: unknown) =>
            segs.reduce<unknown>(
                (o, k) =>
                    o && typeof o === "object" && k in (o as Record<string, unknown>)
                        ? (o as Record<string, unknown>)[k]
                        : undefined,
                root,
            );
        const bv = get(before);
        const av = get(after);
        let status: DiffEntry["status"] = "unchanged";
        if (bv === undefined && av !== undefined) status = "added";
        else if (bv !== undefined && av === undefined) status = "removed";
        else if (JSON.stringify(bv) !== JSON.stringify(av)) status = "changed";
        diff.push({ path: p, before: bv, after: av, status });
    }
    const weight: Record<string, number> = {
        changed: 0,
        added: 1,
        removed: 2,
        unchanged: 3,
    };
    diff.sort(
        (a, b) => weight[a.status] - weight[b.status] || a.path.localeCompare(b.path),
    );
    return diff;
}

export function truncateValue(value: unknown, max = 120): string {
    if (value === null) return "null";
    if (value === undefined) return "undefined";
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function sizeLabel(obj: unknown): string {
    if (!obj) return "0 keys";
    let count = 0;
    const scan = (input: unknown) => {
        if (input && typeof input === "object")
            Object.keys(input as Record<string, unknown>).forEach((key) => {
                count++;
                const child = (input as Record<string, unknown>)[key];
                if (child && typeof child === "object" && !Array.isArray(child))
                    scan(child);
            });
    };
    scan(obj);
    return `${count} keys`;
}
