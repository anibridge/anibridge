<script lang="ts">
    import type { Snippet } from "svelte";

    import { Tooltip } from "bits-ui";

    interface Props {
        children?: Snippet;
        trigger?: Snippet;
        delay?: number;
        class?: string;
        sideOffset?: number;
    }

    let {
        children,
        trigger,
        delay = 100,
        class: className = "z-20 w-min min-w-[8rem] rounded-md border border-(--color-border) bg-(--color-surface-alt)/95 p-2 shadow-xl",
        sideOffset = 8,
    }: Props = $props();
</script>

<Tooltip.Root delayDuration={delay}>
    <Tooltip.Trigger>
        {@render trigger?.()}
    </Tooltip.Trigger>
    <Tooltip.Portal>
        <Tooltip.Content
            {sideOffset}
            class={className}>
            {@render children?.()}
            <Tooltip.Arrow />
        </Tooltip.Content>
    </Tooltip.Portal>
</Tooltip.Root>
