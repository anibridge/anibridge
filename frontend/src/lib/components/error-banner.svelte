<script lang="ts">
    import { RefreshCw, TriangleAlert } from "@lucide/svelte";

    import Button from "$lib/ui/button.svelte";

    interface Props {
        message: string;
        retry?: () => void;
        class?: string;
    }

    let { message, retry, class: className = "" }: Props = $props();
</script>

<div
    class="flex items-center gap-3 rounded-md border border-(--color-danger)/30 bg-(--color-danger)/10 px-4 py-3 text-sm {className}">
    <TriangleAlert class="h-5 w-5 shrink-0 text-(--color-danger)" />
    <span class="flex-1 text-(--color-accent-foreground)">{message}</span>
    {#if retry}
        <Button
            variant="ghost"
            size="sm"
            onclick={retry}>
            <RefreshCw class="h-3.5 w-3.5" />
            Retry
        </Button>
    {/if}
</div>
