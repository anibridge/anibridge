<script lang="ts">
    import type { Snippet } from "svelte";

    import { LoaderCircle } from "@lucide/svelte";

    type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
    type ButtonSize = "sm" | "md" | "lg";

    interface Props {
        children?: Snippet;
        variant?: ButtonVariant;
        size?: ButtonSize;
        disabled?: boolean;
        loading?: boolean;
        type?: "button" | "submit" | "reset";
        class?: string;
        onclick?: (e: MouseEvent) => void;
    }

    let {
        children,
        variant = "secondary",
        size = "md",
        disabled = false,
        loading = false,
        type = "button",
        class: className = "",
        onclick,
        ...rest
    }: Props = $props();

    const sizeClasses: Record<ButtonSize, string> = {
        sm: "h-7 px-2 py-1 text-[11px] gap-1.5",
        md: "h-8 px-3 py-1.5 text-xs gap-1.5",
        lg: "h-9 px-4 py-2 text-sm gap-2",
    };

    const variantClasses: Record<ButtonVariant, string> = {
        primary:
            "border-(--color-accent)/60 bg-(--color-accent)/30 text-(--color-accent-foreground) hover:bg-(--color-accent)/40",
        secondary:
            "border-(--color-border) bg-(--color-surface) text-(--color-muted-foreground) hover:bg-(--color-surface-alt) hover:text-(--color-foreground)",
        danger: "border-(--color-danger)/60 bg-(--color-danger)/20 text-red-200 hover:bg-(--color-danger)/30",
        ghost: "border-transparent text-(--color-muted-foreground) hover:bg-(--color-surface) hover:text-(--color-foreground)",
    };
</script>

<button
    {type}
    {onclick}
    {disabled}
    class="inline-flex items-center rounded-md border font-medium transition-colors focus-visible:ring-2 focus-visible:ring-(--color-accent)/50 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 {sizeClasses[
        size
    ]} {variantClasses[variant]} {className}"
    {...rest}>
    {#if loading}
        <LoaderCircle class="inline h-3.5 w-3.5 animate-spin" />
    {/if}
    {@render children?.()}
</button>
