<script lang="ts">
    import type { Component, Snippet } from "svelte";

    interface Props {
        icon: Component;
        title: string;
        description?: string;
        actions?: Snippet;
        children?: Snippet;
        extra?: Snippet;
    }

    let {
        icon: Icon,
        title,
        description = "",
        actions,
        children,
        extra,
    }: Props = $props();
</script>

<div class="space-y-2">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div class="space-y-1 sm:flex-1">
            <div class="flex items-center gap-2">
                <Icon class="h-4 w-4 text-muted-foreground" />
                <h2 class="text-lg font-semibold">{title}</h2>
            </div>
            {#if description}
                <p class="text-xs text-muted-foreground">{description}</p>
            {/if}
        </div>
        {#if actions}
            <div class="flex flex-wrap items-center gap-2 text-xs sm:justify-end">
                {@render actions()}
            </div>
        {/if}
        {#if extra}
            <div class="w-full sm:w-auto sm:flex-1">
                {@render extra()}
            </div>
        {/if}
    </div>
    {@render children?.()}
</div>
