<script lang="ts">
    interface Props {
        lines?: number;
        class?: string;
    }

    let { lines = 3, class: className = "" }: Props = $props();

    const range = $derived(Array.from({ length: lines }, (_, i) => i));
</script>

<div class="space-y-2 {className}">
    {#each range as i (i)}
        <div
            class="h-4 w-full rounded-md bg-surface animate-pulse"
            style="animation-duration: 2s;">
        </div>
    {/each}
</div>
