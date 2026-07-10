<script lang="ts">
    import { onMount } from "svelte";

    import { ArrowRight, LoaderCircle, Search } from "@lucide/svelte";
    import { Popover } from "bits-ui";

    import { loadCapabilities, type FieldCapability } from "./capabilities-cache";

    interface Props {
        value?: string;
        autoFocus?: boolean;
        disabled?: boolean;
        placeholder?: string;
        size?: "sm" | "md";
        onSubmit?: () => void;
        loading?: boolean;
        onCancel?: () => void;
    }

    let {
        value = $bindable(""),
        autoFocus = false,
        disabled = false,
        placeholder = "Search...",
        size = "sm",
        onSubmit,
        loading = false,
        onCancel,
    }: Props = $props();

    let capabilities = $state<FieldCapability[] | null>(null);
    let KEYS: FieldCapability[] = $derived(capabilities ?? []);

    let inputEl = $state<HTMLInputElement | null>(null);
    let containerEl = $state<HTMLDivElement | null>(null);
    let open = $state(false);
    let activeIndex = $state<number>(-1);
    let caret = $state(0);
    let suggestions = $derived(getSuggestions(value, caret));
    let focused = $state(false);
    const isActive = $derived(focused || open || value.trim().length > 0);
    let pointerInPopover = $state(false);
    const listId = `booru-suggestions-${Math.random().toString(36).slice(2, 8)}`;

    const ANILIST_WARNING_STORAGE_KEY = "mappings.anilist-warning-dismissed.v1";
    let warningDismissed = $state(false);
    const showAniListWarning = $derived(
        /\banilist\./i.test(value) && !warningDismissed,
    );

    type Suggestion = {
        label: string;
        detail?: string;
        kind: "key" | "value" | "op" | "helper";
        apply: (ctx: ApplyCtx) => void;
    };

    type ApplyCtx = {
        value: string;
        caret: number;
        // Replace a range [start, end) with text and optionally place caret
        replace: (
            start: number,
            end: number,
            text: string,
            caretDelta?: number,
        ) => void;
        // Insert text at caret
        insert: (text: string) => void;
        // Set new caret
        setCaret: (pos: number) => void;
    };

    function isBoundary(ch: string) {
        return /\s|\||\(|\)/.test(ch);
    }

    function findSegment(value: string, caret: number) {
        const end = caret;
        let start = end;
        while (start > 0 && !isBoundary(value[start - 1])) start--;
        // include leading ~ or - if directly adjacent
        if (start > 0 && (value[start - 1] === "-" || value[start - 1] === "~")) {
            // but only include if there's boundary before that or it's at index 0
            if (start - 1 === 0 || isBoundary(value[start - 2] || " ")) {
                start -= 1;
            }
        }
        const text = value.slice(start, end);
        return { start, end, text };
    }

    function getKeyInfo(name: string | undefined) {
        if (!name) return undefined;
        const lname = name.toLowerCase();
        for (const k of KEYS) {
            const aliases = (k.aliases ?? []).map((alias) => alias.toLowerCase());
            if (k.key === lname || aliases.includes(lname)) return k;
        }
        return undefined;
    }

    function shouldQuote(cap: FieldCapability | undefined): boolean {
        if (!cap) return false;
        const type = (cap.type || "").toLowerCase();
        return type === "string" || type === "enum";
    }

    function normalizeValuePart(raw: string): string {
        if (!raw) return raw;
        return raw.replace(/^['"]/u, "").replace(/['"]$/u, "");
    }

    function getSuggestions(val: string, caret: number): Suggestion[] {
        const seg = findSegment(val, caret);
        const t = seg.text;

        const out: Suggestion[] = [];

        // If segment looks like key:value (possibly partial value)
        const mKV = t.match(/^([-~]?)([a-zA-Z_][\w.]*)?(:)?([^\s]*)$/);
        if (mKV) {
            const prefix = mKV[1] || ""; // ~ or -
            const name = (mKV[2] || "").toLowerCase();
            const hasColon = !!mKV[3];
            const vpart = mKV[4] || "";
            const kinfo = getKeyInfo(name);
            const needsQuotes = shouldQuote(kinfo);
            const matchPart = needsQuotes ? normalizeValuePart(vpart) : vpart;

            // Key suggestions (when typing name or missing colon)
            if (!hasColon) {
                const needle = name;
                if (!needle && prefix === "" && t.trim() === t) {
                    out.push({
                        label: '"search anilist"',
                        detail: "AniList title search",
                        kind: "helper",
                        apply: ({ replace }) => {
                            replace(seg.start, seg.end, '""');
                        },
                    });
                }

                for (const k of KEYS) {
                    if (needle && !k.key.startsWith(needle)) continue;
                    out.push({
                        label: `${prefix}${k.key}:`,
                        detail: k.desc ?? undefined,
                        kind: "key",
                        apply: ({ replace }) => {
                            replace(seg.start, seg.end, `${prefix}${k.key}:`);
                        },
                    });
                }
            } else if (kinfo) {
                // Operators based on backend capabilities
                const base = `${prefix}${kinfo.key}:`;
                const capOps: string[] = Array.isArray(kinfo.operators)
                    ? kinfo.operators
                    : [];
                const opMap: Record<string, string> = {
                    "=": "Equals",
                    ">": "Greater than",
                    ">=": "Greater or equal",
                    "<": "Less than",
                    "<=": "Less or equal",
                    "*": "Wildcard (*)",
                    "?": "Wildcard (?)",
                    in: "In (a,b,c)",
                    range: "Range (lo..hi)",
                };

                // Transform to UI suffixes
                const allOps: Array<[string, string]> = capOps.map((op) => [
                    op === "=" ? "" : op === "in" ? "," : op === "range" ? ".." : op,
                    op === "wildcard" ? "*" : opMap[op] || "",
                ]);

                if (Array.isArray(kinfo.values) && kinfo.values.length) {
                    const filteredValues = matchPart
                        ? kinfo.values.filter((v) =>
                              v.toLowerCase().startsWith(matchPart.toLowerCase()),
                          )
                        : kinfo.values;
                    for (const val of filteredValues) {
                        const quotedVal = needsQuotes ? `"${val}"` : val;
                        const insertion = base + quotedVal;
                        out.push({
                            label: insertion,
                            kind: "value",
                            apply: ({ replace }) => {
                                replace(seg.start, seg.end, insertion);
                            },
                        });
                    }

                    // remove the '=' operator since we have direct value suggestions
                    const eqIndex = allOps.findIndex(([op]) => op === "");
                    if (eqIndex >= 0) {
                        allOps.splice(eqIndex, 1);
                    }
                }

                const filteredOps = matchPart
                    ? allOps.filter(([op]) => op.startsWith(matchPart))
                    : allOps;
                for (const [op, detail] of filteredOps) {
                    const baseWithOp = (() => {
                        if (needsQuotes && op === "") {
                            return `${base}""`;
                        }
                        return base + op;
                    })();
                    const caretDelta = needsQuotes && op === "" ? -1 : 0;
                    out.push({
                        label: baseWithOp,
                        detail: detail || undefined,
                        kind: "value",
                        apply: ({ replace }) => {
                            const rest = vpart;
                            let next = baseWithOp;
                            if (op === ".." && /\d$/.test(rest)) {
                                next = base + rest + "..";
                            }
                            replace(seg.start, seg.end, next, caretDelta);
                        },
                    });
                }
            }
        }

        if (val.trim() === "") {
            const added: Record<string, true> = {};
            const addKeySuggestion = (cap: FieldCapability | undefined) => {
                if (!cap) return;
                if (added[cap.key]) return;
                out.push({
                    label: `${cap.key}:`,
                    detail: cap.desc ?? undefined,
                    kind: "key",
                    apply: ({ insert }) => insert(`${cap.key}:`),
                });
                added[cap.key] = true;
            };

            for (const k of KEYS) {
                addKeySuggestion(k);
            }
        }

        // De-duplicate by label
        const seen: Record<string, true> = {};
        const uniq: Suggestion[] = [];
        for (const s of out) {
            if (seen[s.label]) continue;
            seen[s.label] = true;
            uniq.push(s);
        }
        return uniq;
    }

    function applySuggestion(idx: number) {
        if (idx < 0 || idx >= suggestions.length) return;
        const applyCtx: ApplyCtx = {
            value,
            caret,
            replace(start, end, text, caretDelta = 0) {
                const left = value.slice(0, start);
                const right = value.slice(end);
                value = left + text + right;
                const nextCaret = start + text.length + caretDelta;
                caret = nextCaret;
                queueMicrotask(() => setSelection(nextCaret));
            },
            insert(text) {
                const left = value.slice(0, caret);
                const right = value.slice(caret);
                value = left + text + right;
                const nextCaret = caret + text.length;
                caret = nextCaret;
                queueMicrotask(() => setSelection(nextCaret));
            },
            setCaret(pos) {
                caret = pos;
                queueMicrotask(() => setSelection(pos));
            },
        };
        suggestions[idx].apply(applyCtx);
        open = true;
        activeIndex = -1;
    }

    function setSelection(pos: number) {
        if (!inputEl) return;
        inputEl.selectionStart = inputEl.selectionEnd = pos;
        inputEl.focus();
    }

    function handleKeydown(e: KeyboardEvent) {
        if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            open = true;
        }
        if (open && suggestions.length) {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                activeIndex = (activeIndex + 1) % suggestions.length;
                return;
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                activeIndex =
                    (activeIndex - 1 + suggestions.length) % suggestions.length;
                return;
            }
            if (e.key === "Enter" || e.key === "Tab") {
                if (activeIndex >= 0) {
                    e.preventDefault();
                    applySuggestion(activeIndex);
                    return;
                }
            }
            if (e.key === "Escape") {
                open = false;
                activeIndex = -1;
                return;
            }
        }
        if (e.key === "Enter") {
            if (activeIndex < 0) {
                e.preventDefault();
                open = false;
                activeIndex = -1;
                onSubmit?.();
            }
        }
    }

    function onInput(ev: Event) {
        const t = ev.target as HTMLInputElement;
        value = t.value;
        caret = t.selectionStart ?? value.length;
        open = true;
        activeIndex = -1;
    }

    function onFocus() {
        focused = true;
        open = true;
        caret = inputEl?.selectionStart ?? 0;
    }

    function onBlur() {
        setTimeout(() => {
            focused = false;
            if (!pointerInPopover) {
                open = false;
                activeIndex = -1;
            }
        }, 50);
    }

    onMount(async () => {
        try {
            const stored = localStorage.getItem(ANILIST_WARNING_STORAGE_KEY);
            if (stored === "1") {
                warningDismissed = true;
            }
        } catch {}

        if (autoFocus) inputEl?.focus();
        try {
            const fields = await loadCapabilities();
            capabilities = fields ? [...fields] : [];
        } catch {}
    });

    function dismissAniListWarning() {
        warningDismissed = true;
        try {
            localStorage.setItem(ANILIST_WARNING_STORAGE_KEY, "1");
        } catch {}
    }

    function handleActionClick() {
        if (loading) {
            onCancel?.();
            return;
        }
        closePopover();
        onSubmit?.();
    }

    function closePopover() {
        open = false;
        activeIndex = -1;
    }

    $effect(() => {
        if (!open) return;
        if (activeIndex < 0) return;
        const el = document.querySelector(
            `[data-booru-suggestion="${activeIndex}"]`,
        ) as HTMLElement | null;
        el?.scrollIntoView({ block: "nearest" });
    });
</script>

<div
    class="relative w-full"
    data-component="booru-search"
    aria-busy={loading}>
    <div
        bind:this={containerEl}
        class="relative flex items-center">
        <div
            class={`relative ml-auto transition-all duration-300 ease-in-out ${
                isActive ? "w-full" : "w-full sm:w-64 md:w-48"
            }`}>
            <input
                bind:this={inputEl}
                {placeholder}
                {disabled}
                {value}
                oninput={onInput}
                onkeydown={handleKeydown}
                onfocus={onFocus}
                onblur={onBlur}
                role="combobox"
                aria-expanded={open}
                aria-controls={listId}
                aria-label="Search mappings"
                class={`
                    w-full
                    ${size === "sm" ? "h-8 pr-9 pl-8" : "h-9 pr-10 pl-9"}
                    rounded-md 
                    border border-slate-700/70 
                    bg-slate-900/70 
                    text-[11px] 
                    shadow-sm 
                    placeholder:text-slate-500 
                    focus:border-slate-600 
                    focus:bg-slate-900
                    focus:outline-none
                `} />
            <Search
                class={`
                    pointer-events-none 
                    absolute 
                    top-1/2 
                    left-2.5 
                    -translate-y-1/2 
                    text-slate-500
                    ${size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"}
                `} />
            <button
                class={`
                    absolute 
                    top-1/2 
                    right-1 
                    inline-flex
                    -translate-y-1/2 
                    items-center 
                    justify-center 
                    rounded-md 
                    bg-slate-800 
                    text-slate-300 
                    transition-colors
                    hover:bg-slate-700
                    ${size === "sm" ? "h-6 w-6" : "h-7 w-7"}
                `}
                type="button"
                aria-label={loading ? "Cancel search" : "Run search"}
                title={loading ? "Cancel search" : "Run search"}
                onclick={handleActionClick}
                aria-busy={loading}
                disabled={disabled && !loading}>
                {#if loading}
                    <LoaderCircle
                        class={`${size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5"} animate-spin`} />
                {:else}
                    <ArrowRight class={size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5"} />
                {/if}
            </button>
        </div>
    </div>

    <Popover.Root
        bind:open
        onOpenChangeComplete={(o: boolean) => {
            if (!o) {
                activeIndex = -1;
                pointerInPopover = false;
            }
        }}>
        <Popover.Portal>
            <Popover.Content
                customAnchor={inputEl}
                side="bottom"
                align="end"
                sideOffset={6}
                trapFocus={false}
                onOpenAutoFocus={(e) => e.preventDefault()}
                updatePositionStrategy="always"
                class="focus-override data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 z-50 mt-1 w-(--bits-popover-anchor-width) max-w-[90vw] overflow-hidden rounded-md border border-slate-800/70 bg-slate-900/95 shadow-xl outline-hidden backdrop-blur supports-backdrop-filter:bg-slate-900/75"
                onmouseenter={() => (pointerInPopover = true)}
                onmouseleave={() => {
                    pointerInPopover = false;
                    if (!focused) closePopover();
                }}>
                <ul
                    id={listId}
                    role="listbox"
                    class={"max-h-64 overflow-auto py-1 text-[11px]" +
                        (suggestions.length === 0 ? " hidden" : "")}>
                    {#each suggestions as s, i (s.label)}
                        <li>
                            <button
                                data-booru-suggestion={i}
                                role="option"
                                aria-selected={i === activeIndex}
                                class={`group flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left hover:bg-slate-800/70 ${i === activeIndex ? "bg-slate-800/70" : ""}`}
                                onmousedown={(e) => {
                                    e.preventDefault();
                                    applySuggestion(i);
                                }}>
                                <span class="font-mono text-slate-200">{s.label}</span>
                                {#if s.detail}
                                    <span
                                        class="ml-2 shrink-0 text-[10px] text-slate-400"
                                        >{s.detail}</span>
                                {/if}
                            </button>
                        </li>
                    {/each}
                </ul>
                <div
                    class="border-t border-slate-800/70 px-2 py-1 text-[10px] text-slate-400">
                    Tips: Use '()' to group terms, '|' to OR between groups, '~' to OR
                    within a group, '-' to negate, '..' for ranges, '*' or '?' as
                    wildcards, and quotes for AniList title search.
                </div>
                {#if showAniListWarning}
                    <div
                        class="border-t border-slate-800/70 bg-yellow-900/90 px-2 py-1 text-[10px] text-yellow-100 ring-1 ring-yellow-700/70">
                        <div class="flex items-start justify-between gap-2">
                            <span class="leading-snug">
                                Warning: querying AniList fields can cause long response
                                times and excessive API usage if the query covers many
                                entries. Consider narrowing your search criteria when
                                possible to improve performance.
                            </span>
                            <button
                                type="button"
                                class="ml-auto flex shrink-0 items-center rounded bg-amber-500/20 px-2 py-0.5 text-[10px] font-medium text-amber-50 transition hover:bg-amber-500/30"
                                onclick={dismissAniListWarning}>
                                Dismiss
                            </button>
                        </div>
                    </div>
                {/if}
            </Popover.Content>
        </Popover.Portal>
    </Popover.Root>
</div>
