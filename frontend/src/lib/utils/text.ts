export function titleCase(str: string | null | undefined): string {
    return (str ?? "")
        .toLowerCase()
        .split(/[\s_-]+/)
        .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
        .join(" ");
}
