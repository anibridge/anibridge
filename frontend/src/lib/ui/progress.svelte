<script lang="ts">
    import { Meter } from "bits-ui";

    interface Props {
        value?: number; // 0-1
        indeterminate?: boolean;
        class?: string;
    }

    let { value = 0, indeterminate = false, class: className = "" }: Props = $props();

    const percent = $derived(Math.max(0, Math.min(1, value)));
</script>

<Meter.Root
    {value}
    max={1}
    class="relative h-1.5 w-full overflow-hidden rounded-full bg-surface {className}">
    <div
        class="h-full rounded-full bg-accent transition-transform duration-300 ease-out"
        class:animate-progress-indeterminate={indeterminate}
        style="transform: translateX(-{indeterminate ? 0 : 100 - 100 * percent}%)">
    </div>
</Meter.Root>
