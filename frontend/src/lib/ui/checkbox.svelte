<script lang="ts">
    import { Check } from "@lucide/svelte";
    import { Checkbox } from "bits-ui";

    interface Props {
        checked?: boolean;
        disabled?: boolean;
        label?: string;
        class?: string;
        oncheckedchange?: (checked: boolean) => void;
    }

    let {
        checked = $bindable(false),
        disabled = false,
        label = "",
        class: className = "",
        oncheckedchange,
    }: Props = $props();
</script>

<label
    class="flex items-center gap-2 {disabled
        ? 'cursor-not-allowed opacity-50'
        : 'cursor-pointer'} {className}">
    <Checkbox.Root
        bind:checked
        {disabled}
        onCheckedChange={oncheckedchange}
        class="flex h-4 w-4 shrink-0 items-center justify-center rounded border-border bg-bg-alt data-[state=checked]:border-accent data-[state=checked]:bg-accent">
        {#snippet children({ checked: isChecked })}
            {#if isChecked}
                <Check class="h-3 w-3 text-accent-foreground" />
            {/if}
        {/snippet}
    </Checkbox.Root>
    {#if label}
        <span class="text-sm text-muted-foreground select-none">{label}</span>
    {/if}
</label>
