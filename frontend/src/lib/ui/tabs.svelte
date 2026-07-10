<script lang="ts">
    import type { Snippet } from "svelte";

    import { Tabs } from "bits-ui";

    interface Props {
        value?: string;
        onValueChange?: (value: string) => void;
        class?: string;
        children?: Snippet;
        listClass?: string;
        listChildren?: Snippet;
    }

    let {
        value = $bindable(""),
        onValueChange,
        class: className = "",
        children,
        listClass = "",
        listChildren,
    }: Props = $props();
</script>

<Tabs.Root
    bind:value
    {onValueChange}
    class={className}>
    {#if listChildren}
        <Tabs.List class="flex items-center gap-1 {listClass}">
            {@render listChildren?.()}
        </Tabs.List>
    {/if}
    {@render children?.()}
</Tabs.Root>
