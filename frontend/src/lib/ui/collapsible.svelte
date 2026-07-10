<script lang="ts">
    import type { Snippet } from "svelte";

    import { ChevronDown } from "@lucide/svelte";
    import { Collapsible } from "bits-ui";

    interface Props {
        trigger?: Snippet;
        children?: Snippet;
        open?: boolean;
        showArrow?: boolean;
        class?: string;
        contentClass?: string;
    }

    let {
        trigger,
        children,
        open = $bindable(false),
        showArrow = false,
        class: className = "",
        contentClass = "",
    }: Props = $props();
</script>

<Collapsible.Root
    bind:open
    class={className}>
    <Collapsible.Trigger class="flex w-full items-center gap-2">
        {@render trigger?.()}
        {#if showArrow}
            <ChevronDown
                class="ml-auto h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200 {open
                    ? 'rotate-180'
                    : ''}" />
        {/if}
    </Collapsible.Trigger>
    <Collapsible.Content
        class="overflow-hidden data-[state=closed]:animate-collapse-up data-[state=open]:animate-collapse-down {contentClass}">
        {@render children?.()}
    </Collapsible.Content>
</Collapsible.Root>
