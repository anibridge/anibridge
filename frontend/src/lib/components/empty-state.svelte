<script lang="ts">
    import type { Component } from "svelte";

    interface Props {
        icon?: Component;
        title: string;
        description?: string;
        class?: string;
    }

    let {
        icon: Icon,
        title,
        description = "",
        class: className = "",
    }: Props = $props();
</script>

<div
    class="flex flex-col items-center justify-center rounded-md border-2 border-dashed border-border bg-bg-alt/40 p-8 text-center transition-colors hover:border-border-muted hover:bg-bg-alt/60 {className}">
    {#if Icon}
        <div class="mb-3 text-dimmed">
            <Icon class="h-8 w-8" />
        </div>
    {/if}
    <p class="text-sm font-medium text-muted-foreground">{title}</p>
    {#if description}
        <p class="mt-1 text-xs text-dimmed">{description}</p>
    {/if}
</div>
