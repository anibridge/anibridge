<script lang="ts">
    import type { Snippet } from "svelte";

    import { Popover } from "bits-ui";

    interface Props {
        trigger?: Snippet;
        children?: Snippet;
        open?: boolean;
        sideOffset?: number;
        align?: "start" | "center" | "end";
        contentClass?: string;
    }

    let {
        trigger,
        children,
        open = $bindable(false),
        sideOffset = 6,
        align = "start",
        contentClass = "",
    }: Props = $props();
</script>

<Popover.Root bind:open>
    <Popover.Trigger>
        {@render trigger?.()}
    </Popover.Trigger>
    <Popover.Content
        {sideOffset}
        {align}
        class="z-50 min-w-[10rem] rounded-md border border-(--color-border) bg-(--color-surface-alt)/95 p-2 shadow-lg backdrop-blur-sm {contentClass}">
        {@render children?.()}
    </Popover.Content>
</Popover.Root>
