<script lang="ts">
    import { onDestroy } from "svelte";

    import { X } from "@lucide/svelte";

    import { dismiss, toasts, type Toast } from "$lib/utils/notify";

    let list: Toast[] = $state([]);
    const unsub = toasts.subscribe((v) => (list = v));

    onDestroy(() => unsub());

    const COLORS: Record<string, string> = {
        info: "border-(--color-accent)/60 bg-(--color-accent-muted)/40 text-(--color-accent-foreground)",
        success: "border-(--color-success)/60 bg-(--color-success)/20 text-emerald-100",
        error: "border-(--color-danger)/60 bg-(--color-danger)/20 text-red-100",
        warn: "border-(--color-warning)/60 bg-(--color-warning)/20 text-amber-100",
    };
</script>

<div
    class="pointer-events-none fixed top-20 right-2 z-60 flex w-80 max-w-[92vw] flex-col gap-2 sm:top-16 sm:right-4"
    aria-live="assertive"
    aria-relevant="additions removals">
    {#each list as t (t.id)}
        <div
            class={`group pointer-events-auto relative flex overflow-hidden rounded-md border p-3 pr-8 text-sm shadow-lg backdrop-blur fade-in ${COLORS[t.type]}`}
            role="alert">
            <span class="block cursor-text leading-snug select-text">{t.message}</span>
            <button
                type="button"
                title="Dismiss"
                class="pointer-events-auto absolute top-1.5 right-1.5 inline-flex h-6 w-6 items-center justify-center rounded-md bg-black/20 text-(--color-dimmed) transition-colors select-none hover:bg-black/40 hover:text-(--color-foreground)"
                onclick={() => dismiss(t.id)}>
                <X class="inline h-3.5 w-3.5" />
            </button>
            <div
                class="pointer-events-none absolute bottom-0 left-0 h-0.5 w-full bg-black/20">
                <div
                    class="h-full bg-white/30"
                    style={`animation: shrink ${t.timeout}ms linear forwards`}>
                </div>
            </div>
        </div>
    {/each}
</div>

<style>
    @keyframes shrink {
        from {
            width: 100%;
        }
        to {
            width: 0%;
        }
    }
</style>
