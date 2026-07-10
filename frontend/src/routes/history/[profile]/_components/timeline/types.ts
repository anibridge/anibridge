import {
    Ban,
    CalendarClock,
    Circle,
    CircleCheck,
    Clock3,
    FileText,
    Layers,
    Radio,
    RotateCcw,
    RotateCw,
    Trash2,
    TriangleAlert,
    Undo2,
} from "@lucide/svelte";

export type TimelineTone = "slate" | "emerald" | "sky" | "amber" | "rose" | "indigo";

export interface OutcomeMeta {
    label: string;
    color: string;
    icon: typeof Circle;
    order: number;
    tone: TimelineTone;
    badgeClass: string;
}

export interface ResourceMeta {
    label: string;
    icon: typeof Circle;
    order: number;
}

export interface ActionMeta {
    label: string;
    icon: typeof Circle;
}

export const OUTCOME_META: Record<string, OutcomeMeta> = {
    synced: {
        label: "Synced",
        color: "emerald",
        icon: CircleCheck,
        order: 10,
        tone: "emerald",
        badgeClass: "border-emerald-700/60 bg-emerald-600/20 text-emerald-200",
    },
    deleted: {
        label: "Deleted",
        color: "amber",
        icon: Trash2,
        order: 20,
        tone: "amber",
        badgeClass: "border-amber-700/60 bg-amber-600/20 text-amber-200",
    },
    undone: {
        label: "Undone",
        color: "sky",
        icon: Undo2,
        order: 30,
        tone: "sky",
        badgeClass: "border-sky-700/60 bg-sky-600/20 text-sky-200",
    },
    pending: {
        label: "Pending",
        color: "indigo",
        icon: Clock3,
        order: 40,
        tone: "indigo",
        badgeClass: "border-indigo-700/60 bg-indigo-600/20 text-indigo-200",
    },
    skipped: {
        label: "Skipped",
        color: "slate",
        icon: Ban,
        order: 50,
        tone: "slate",
        badgeClass: "border-slate-700/70 bg-slate-800/60 text-slate-300",
    },
    not_found: {
        label: "Not Found",
        color: "amber",
        icon: TriangleAlert,
        order: 60,
        tone: "amber",
        badgeClass: "border-amber-700/70 bg-amber-600/20 text-amber-200",
    },
    failed: {
        label: "Failed",
        color: "rose",
        icon: TriangleAlert,
        order: 70,
        tone: "rose",
        badgeClass: "border-rose-700/70 bg-rose-600/20 text-rose-200",
    },
};

export const RESOURCE_META: Record<string, ResourceMeta> = {
    record: { label: "Records", icon: FileText, order: 10 },
    event: { label: "Events", icon: CalendarClock, order: 20 },
    node: { label: "Nodes", icon: Radio, order: 30 },
};

export const ACTION_META: Record<string, ActionMeta> = {
    sync: { label: "Sync", icon: RotateCw },
    create: { label: "Create", icon: CircleCheck },
    update: { label: "Update", icon: RotateCcw },
    delete: { label: "Delete", icon: Trash2 },
    undo: { label: "Undo", icon: Undo2 },
};

export const DEFAULT_OUTCOME_META: OutcomeMeta = {
    label: "Other",
    color: "slate",
    icon: Circle,
    order: 999,
    tone: "slate",
    badgeClass: "border-slate-700/70 bg-slate-800/60 text-slate-300",
};

export const DEFAULT_RESOURCE_META: ResourceMeta = {
    label: "Other",
    icon: Layers,
    order: 999,
};

export function outcomeMeta(outcome?: string | null): OutcomeMeta {
    if (!outcome) return DEFAULT_OUTCOME_META;
    return (
        OUTCOME_META[outcome] ?? {
            ...DEFAULT_OUTCOME_META,
            label: outcome.replaceAll("_", " "),
        }
    );
}

export function resourceMeta(resourceKind?: string | null): ResourceMeta {
    if (!resourceKind) return DEFAULT_RESOURCE_META;
    return (
        RESOURCE_META[resourceKind] ?? {
            ...DEFAULT_RESOURCE_META,
            label: resourceKind.replaceAll("_", " "),
        }
    );
}
