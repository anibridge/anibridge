<script lang="ts">
    import type { Snippet } from "svelte";

    import { X } from "@lucide/svelte";
    import { Dialog } from "bits-ui";

    interface Props {
        children?: Snippet;
        titleChildren?: Snippet;
        footerChildren?: Snippet;
        open: boolean;
        overlayClass?: string;
        contentClass?: string;
        bodyClass?: string;
        headerClass?: string;
        headerWrapperClass?: string;
        footerClass?: string;
        titleClass?: string;
        closeButtonClass?: string;
        showClose?: boolean;
        onOpenAutoFocus?: (e: Event) => void;
        onCloseAutoFocus?: (e: Event) => void;
    }

    let {
        children,
        titleChildren,
        footerChildren,
        open = $bindable(false),
        overlayClass = "fixed inset-0 z-40 bg-black/70 backdrop-blur-sm",
        contentClass = "fixed top-1/2 left-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-md border border-(--color-border) bg-(--color-surface-alt)/95 shadow-xl ring-1 ring-(--color-border)/40",
        bodyClass = "",
        headerClass = "flex items-start justify-between gap-4",
        headerWrapperClass = "p-4",
        footerClass = "",
        titleClass = "flex items-center gap-2 text-sm font-semibold tracking-wide",
        closeButtonClass = "text-(--color-muted-foreground) hover:text-(--color-foreground)",
        showClose = true,
        onOpenAutoFocus = (e: Event) => e.preventDefault(),
        onCloseAutoFocus = undefined,
    }: Props = $props();
</script>

<Dialog.Root bind:open>
    <Dialog.Portal>
        <Dialog.Overlay class={overlayClass} />
        <Dialog.Content
            class={contentClass}
            {onOpenAutoFocus}
            {onCloseAutoFocus}>
            {#if titleChildren}
                <div class={headerWrapperClass}>
                    <div class={headerClass}>
                        <Dialog.Title class={titleClass}>
                            {@render titleChildren?.()}
                        </Dialog.Title>
                        {#if showClose}
                            <Dialog.Close
                                class={closeButtonClass}
                                aria-label="Close">
                                <X class="inline h-3.5 w-3.5" />
                            </Dialog.Close>
                        {/if}
                    </div>
                </div>
            {/if}

            <div class={bodyClass}>
                {@render children?.()}
            </div>

            <div class={footerClass}>
                {@render footerChildren?.()}
            </div>
        </Dialog.Content>
    </Dialog.Portal>
</Dialog.Root>
