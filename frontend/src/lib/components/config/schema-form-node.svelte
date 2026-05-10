<script lang="ts">
    import { CirclePlus, Info, Trash2 } from "@lucide/svelte";

    import Tooltip from "$lib/ui/tooltip.svelte";
    import {
        asRecord,
        deepClone,
        getAdditionalPropertiesSchema,
        getAnyOfOptions,
        getArrayItemSchema,
        getObjectProperties,
        getPreferredSchema,
        getRequiredKeys,
        humanizeKey,
        makeDefaultValue,
        resolveSchema,
        schemaMatchesValue,
        type JsonPath,
        type SchemaObject,
    } from "./schema-form";
    import SchemaFormNode from "./schema-form-node.svelte";

    interface Props {
        rootSchema: SchemaObject;
        schema: SchemaObject;
        value?: unknown;
        path?: JsonPath;
        label?: string;
        required?: boolean;
        depth?: number;
        showHeader?: boolean;
        addLabel?: string;
        suppressHeaderlessAddAction?: boolean;
        suppressHeaderlessUnionSelector?: boolean;
        onChange: (path: JsonPath, value: unknown) => void;
        onDelete?: (path: JsonPath) => void;
    }

    let {
        rootSchema,
        schema,
        value = undefined,
        path = [],
        label = undefined,
        required = false,
        depth = 0,
        showHeader = true,
        addLabel = undefined,
        suppressHeaderlessAddAction = false,
        suppressHeaderlessUnionSelector = false,
        onChange,
        onDelete = undefined,
    }: Props = $props();

    const unionSelectorClass =
        "flex max-w-full flex-wrap items-center rounded-md border border-slate-700/80 bg-slate-900/80 p-0.5";

    function getUnionIndex(options: SchemaObject[], candidate: unknown): number {
        return Math.max(
            options.findIndex((option) => schemaMatchesValue(option, candidate)),
            options.length > 0 ? 0 : -1,
        );
    }

    function getUnionOptionLabel(option: SchemaObject, index: number): string {
        if (typeof option.title === "string" && option.title.trim()) {
            return option.title;
        }

        if (typeof option.type === "string" && option.type.trim()) {
            return option.type;
        }

        return `option-${index + 1}`;
    }

    function getUnionOptionDefault(
        option: SchemaObject,
        parentSchema: SchemaObject,
    ): unknown {
        if (
            parentSchema.default !== undefined &&
            schemaMatchesValue(option, parentSchema.default)
        ) {
            return deepClone(parentSchema.default);
        }

        return makeDefaultValue(option, rootSchema);
    }

    const baseSchema = $derived(resolveSchema(schema, rootSchema));
    const resolvedSchema = $derived(getPreferredSchema(schema, rootSchema, value));
    const pathLeaf = $derived(path[path.length - 1]);
    const anyOfOptions = $derived(getAnyOfOptions(schema, rootSchema));
    const currentUnionValue = $derived(
        value === undefined ? baseSchema.default : value,
    );
    const currentUnionIndex = $derived(getUnionIndex(anyOfOptions, currentUnionValue));
    const title = $derived(
        (typeof resolvedSchema.title === "string" && resolvedSchema.title) ||
            label ||
            (typeof pathLeaf === "string" ? humanizeKey(pathLeaf) : undefined),
    );
    const description = $derived(
        typeof resolvedSchema.description === "string"
            ? resolvedSchema.description
            : null,
    );
    const enumOptions = $derived(
        Array.isArray(resolvedSchema.enum)
            ? resolvedSchema.enum.filter((entry): entry is string | number | boolean =>
                  ["string", "number", "boolean"].includes(typeof entry),
              )
            : [],
    );
    const objectProperties = $derived(getObjectProperties(resolvedSchema, rootSchema));
    const requiredKeys = $derived(getRequiredKeys(resolvedSchema, rootSchema));
    const additionalSchema = $derived(
        getAdditionalPropertiesSchema(resolvedSchema, rootSchema),
    );
    const objectValue = $derived(asRecord(value));
    const knownKeys = $derived(Object.keys(objectProperties));
    const extraKeys = $derived(
        Object.keys(objectValue).filter((key) => !knownKeys.includes(key)),
    );
    const itemSchema = $derived(getArrayItemSchema(resolvedSchema, rootSchema));
    const arrayValue = $derived(Array.isArray(value) ? value : []);
    const isObject = $derived(
        resolvedSchema.type === "object" ||
            knownKeys.length > 0 ||
            additionalSchema !== null,
    );
    const isArray = $derived(resolvedSchema.type === "array");
    const isBoolean = $derived(resolvedSchema.type === "boolean");
    const isEnum = $derived(enumOptions.length > 0);
    const isNull = $derived(resolvedSchema.type === "null");
    const isNumeric = $derived(
        resolvedSchema.type === "integer" || resolvedSchema.type === "number",
    );
    const allowsNull = $derived(
        isNull || anyOfOptions.some((option) => option.type === "null"),
    );
    const pathKey = $derived(String(path.join(".")));
    const showHeaderlessUnionSelector = $derived(
        !showHeader && !suppressHeaderlessUnionSelector && anyOfOptions.length > 1,
    );

    function promptForEntryKey(message: string, currentValue: unknown): string | null {
        const nextKey = window.prompt(message);
        const normalized = nextKey?.trim();
        const currentEntryValue = asRecord(currentValue);
        return !normalized || currentEntryValue[normalized] !== undefined
            ? null
            : normalized;
    }

    function addObjectEntry(
        entryPath: JsonPath,
        currentValue: unknown,
        entrySchema: SchemaObject,
        promptMessage: string,
    ) {
        const entryAdditionalSchema = getAdditionalPropertiesSchema(
            entrySchema,
            rootSchema,
        );
        if (!entryAdditionalSchema) return;

        const nextKey = promptForEntryKey(promptMessage, currentValue);
        if (!nextKey) return;

        onChange(
            [...entryPath, nextKey],
            makeDefaultValue(entryAdditionalSchema, rootSchema),
        );
    }

    function promptAddObjectEntry() {
        addObjectEntry(
            path,
            objectValue,
            resolvedSchema,
            `New ${addLabel ?? "entry"} name`,
        );
    }

    function addArrayItem() {
        onChange(path, [...arrayValue, makeDefaultValue(itemSchema, rootSchema)]);
    }

    function addArrayItemAt(
        entryPath: JsonPath,
        currentValue: unknown,
        entrySchema: SchemaObject,
    ) {
        const currentItems = Array.isArray(currentValue) ? currentValue : [];
        const nextItemSchema = getArrayItemSchema(entrySchema, rootSchema);
        onChange(entryPath, [
            ...currentItems,
            makeDefaultValue(nextItemSchema, rootSchema),
        ]);
    }

    function removeArrayItem(index: number) {
        onChange(
            path,
            arrayValue.filter((_, entryIndex) => entryIndex !== index),
        );
    }

    function selectUnionOption(
        entryPath: JsonPath,
        parentSchema: SchemaObject,
        currentValue: unknown,
        options: SchemaObject[],
        nextIndex: number,
    ) {
        const option = options[nextIndex];
        if (!option || schemaMatchesValue(option, currentValue)) return;

        onChange(entryPath, getUnionOptionDefault(option, parentSchema));
    }

    const wrapperClass = $derived(
        depth === 0
            ? "space-y-4"
            : "space-y-4 rounded-md border border-slate-800/70 bg-slate-950/40 p-4",
    );

    const addButtonClass =
        "inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-emerald-200 shadow-sm transition-colors hover:border-emerald-400 hover:text-emerald-100 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-emerald-200";
    const removeButtonClass =
        "inline-flex items-center rounded-md border border-slate-700/60 bg-slate-900/60 p-1 text-[12px] font-semibold text-rose-200 transition-colors hover:border-rose-500 focus:outline-none disabled:pointer-events-none disabled:opacity-50 disabled:hover:border-slate-700/60 disabled:hover:text-rose-200";
</script>

{#snippet renderHeaderDetails(
    headerTitle: string | undefined,
    headerDescription: string | null,
    headerRequired: boolean,
)}
    {#if headerDescription}
        <Tooltip
            class="z-20 max-w-xs rounded-md border border-slate-800/70 bg-slate-900/95 px-2 py-1.5 text-[11px] leading-relaxed text-slate-100 shadow-xl">
            {#snippet trigger()}
                <button
                    type="button"
                    class="inline-flex h-5 w-5 items-center justify-center rounded-full text-slate-500 transition-colors hover:text-slate-200 focus-visible:ring-2 focus-visible:ring-blue-500/40 focus-visible:outline-none"
                    aria-label={`${headerTitle} description`}>
                    <Info class="h-3.5 w-3.5" />
                </button>
            {/snippet}
            {headerDescription}
        </Tooltip>
    {/if}
    {#if headerRequired}
        <span
            class="rounded-full border border-blue-500/40 bg-blue-500/10 px-2 py-0.5 text-[10px] font-medium tracking-wide text-blue-200">
            Required
        </span>
    {/if}
{/snippet}

{#snippet renderUnionSelector(
    options: SchemaObject[],
    activeIndex: number,
    keyPrefix: string,
    entryPath: JsonPath,
    parentSchema: SchemaObject,
    currentValue: unknown,
)}
    <div class={unionSelectorClass}>
        {#each options as option, index (`${keyPrefix}:${index}`)}
            <button
                type="button"
                class={`rounded-md px-2 py-0.5 text-[10px] font-medium transition ${index === activeIndex ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-200"}`}
                onclick={() =>
                    selectUnionOption(
                        entryPath,
                        parentSchema,
                        currentValue,
                        options,
                        index,
                    )}>
                {getUnionOptionLabel(option, index)}
            </button>
        {/each}
    </div>
{/snippet}

{#if isObject}
    <div class={wrapperClass}>
        {#if showHeader && title}
            <div class="space-y-1">
                <div
                    class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div class="flex min-w-0 items-center gap-2">
                        {#if additionalSchema}
                            <button
                                type="button"
                                class={addButtonClass}
                                title={`Add ${addLabel ?? "entry"}`}
                                aria-label={`Add ${addLabel ?? "entry"}`}
                                onclick={promptAddObjectEntry}>
                                <CirclePlus class="inline h-4 w-4" />
                            </button>
                        {/if}
                        <h3 class="truncate text-sm font-semibold text-slate-100">
                            {title}
                        </h3>
                        {@render renderHeaderDetails(title, description, required)}
                    </div>
                    {#if anyOfOptions.length > 1}
                        {@render renderUnionSelector(
                            anyOfOptions,
                            currentUnionIndex,
                            pathKey,
                            path,
                            baseSchema,
                            currentUnionValue,
                        )}
                    {/if}
                </div>
            </div>
        {/if}

        {#if showHeaderlessUnionSelector}
            <div class="flex justify-start">
                {@render renderUnionSelector(
                    anyOfOptions,
                    currentUnionIndex,
                    pathKey,
                    path,
                    baseSchema,
                    currentUnionValue,
                )}
            </div>
        {/if}

        {#if additionalSchema && (!showHeader || !title) && !suppressHeaderlessAddAction}
            <div class="flex justify-end">
                <button
                    type="button"
                    class={addButtonClass}
                    title={`Add ${addLabel ?? "entry"}`}
                    aria-label={`Add ${addLabel ?? "entry"}`}
                    onclick={promptAddObjectEntry}>
                    <CirclePlus class="inline h-4 w-4" />
                </button>
            </div>
        {/if}

        {#each knownKeys as key (key)}
            <SchemaFormNode
                {rootSchema}
                schema={objectProperties[key]}
                value={objectValue[key]}
                path={[...path, key]}
                label={humanizeKey(key)}
                required={requiredKeys.has(key)}
                depth={depth + 1}
                {onChange}
                {onDelete} />
        {/each}

        {#each extraKeys as key (key)}
            {@const extraEntrySchema = additionalSchema ?? {}}
            {@const extraEntryResolvedSchema = getPreferredSchema(
                extraEntrySchema,
                rootSchema,
                objectValue[key],
            )}
            {@const extraEntryCanAdd =
                getAdditionalPropertiesSchema(extraEntryResolvedSchema, rootSchema) !==
                null}
            {@const extraEntryIsArray = extraEntryResolvedSchema.type === "array"}
            {@const extraEntryUnionOptions = getAnyOfOptions(
                extraEntrySchema,
                rootSchema,
            )}
            {@const extraEntryUnionIndex = getUnionIndex(
                extraEntryUnionOptions,
                objectValue[key],
            )}
            <div
                class="space-y-3 rounded-md border border-slate-800/70 bg-slate-950/40 p-4">
                <div
                    class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div class="flex min-w-0 items-center gap-2">
                        {#if extraEntryCanAdd || extraEntryIsArray}
                            <button
                                type="button"
                                class={addButtonClass}
                                title={extraEntryIsArray ? "Add item" : "Add entry"}
                                aria-label={extraEntryIsArray
                                    ? "Add item"
                                    : "Add entry"}
                                onclick={() =>
                                    extraEntryIsArray
                                        ? addArrayItemAt(
                                              [...path, key],
                                              objectValue[key],
                                              extraEntryResolvedSchema,
                                          )
                                        : addObjectEntry(
                                              [...path, key],
                                              objectValue[key],
                                              extraEntryResolvedSchema,
                                              "New entry name",
                                          )}>
                                <CirclePlus class="inline h-4 w-4" />
                            </button>
                        {/if}
                        <h4 class="truncate text-sm font-semibold text-slate-100">
                            {key}
                        </h4>
                    </div>
                    <div
                        class="flex max-w-full flex-wrap items-center gap-2 sm:justify-end">
                        {#if extraEntryUnionOptions.length > 1}
                            {@render renderUnionSelector(
                                extraEntryUnionOptions,
                                extraEntryUnionIndex,
                                `${pathKey}.${key}`,
                                [...path, key],
                                extraEntrySchema,
                                objectValue[key],
                            )}
                        {/if}
                        {#if onDelete}
                            <button
                                type="button"
                                class={removeButtonClass}
                                title="Remove custom entry"
                                aria-label="Remove custom entry"
                                onclick={() => onDelete?.([...path, key])}>
                                <Trash2 class="inline h-4 w-4" />
                            </button>
                        {/if}
                    </div>
                </div>
                <SchemaFormNode
                    {rootSchema}
                    schema={extraEntrySchema}
                    value={objectValue[key]}
                    path={[...path, key]}
                    label={key}
                    depth={depth + 1}
                    showHeader={false}
                    suppressHeaderlessAddAction={extraEntryCanAdd || extraEntryIsArray}
                    suppressHeaderlessUnionSelector={extraEntryUnionOptions.length > 1}
                    {onChange}
                    {onDelete} />
            </div>
        {/each}
    </div>
{:else if isArray}
    <div class={wrapperClass}>
        {#if showHeader && title}
            <div class="space-y-1">
                <div
                    class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div class="flex min-w-0 items-center gap-2">
                        <button
                            type="button"
                            class={addButtonClass}
                            title="Add item"
                            aria-label="Add item"
                            onclick={addArrayItem}>
                            <CirclePlus class="inline h-4 w-4" />
                        </button>
                        <h3 class="truncate text-sm font-semibold text-slate-100">
                            {title}
                        </h3>
                        {@render renderHeaderDetails(title, description, required)}
                    </div>
                    {#if anyOfOptions.length > 1}
                        {@render renderUnionSelector(
                            anyOfOptions,
                            currentUnionIndex,
                            pathKey,
                            path,
                            baseSchema,
                            currentUnionValue,
                        )}
                    {/if}
                </div>
            </div>
        {/if}

        {#if showHeaderlessUnionSelector}
            <div class="flex justify-start">
                {@render renderUnionSelector(
                    anyOfOptions,
                    currentUnionIndex,
                    pathKey,
                    path,
                    baseSchema,
                    currentUnionValue,
                )}
            </div>
        {/if}

        {#if (!showHeader || !title) && !suppressHeaderlessAddAction}
            <div class="flex justify-end">
                <button
                    type="button"
                    class={addButtonClass}
                    title="Add item"
                    aria-label="Add item"
                    onclick={addArrayItem}>
                    <CirclePlus class="inline h-4 w-4" />
                </button>
            </div>
        {/if}

        {#if arrayValue.length > 0}
            <div class="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
                {#each arrayValue as item, index (`${pathKey}:${index}`)}
                    {@const itemUnionOptions = getAnyOfOptions(itemSchema, rootSchema)}
                    {@const itemUnionIndex = getUnionIndex(itemUnionOptions, item)}
                    <div
                        class="min-w-0 flex-1 space-y-3 rounded-md border border-slate-800/70 bg-slate-950/40 p-4 sm:min-w-[18rem]">
                        <div
                            class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                            <div class="flex min-w-0 items-center gap-2">
                                <p class="text-xs font-medium text-slate-300">
                                    #{index + 1}
                                </p>
                            </div>
                            <div
                                class="flex max-w-full flex-wrap items-center gap-2 sm:justify-end">
                                {#if itemUnionOptions.length > 1}
                                    {@render renderUnionSelector(
                                        itemUnionOptions,
                                        itemUnionIndex,
                                        `${pathKey}:${index}`,
                                        [...path, index],
                                        itemSchema,
                                        item,
                                    )}
                                {/if}
                                <button
                                    type="button"
                                    class={removeButtonClass}
                                    title="Remove item"
                                    aria-label="Remove item"
                                    onclick={() => removeArrayItem(index)}>
                                    <Trash2 class="inline h-4 w-4" />
                                </button>
                            </div>
                        </div>
                        <SchemaFormNode
                            {rootSchema}
                            schema={itemSchema}
                            value={item}
                            path={[...path, index]}
                            depth={depth + 1}
                            showHeader={false}
                            suppressHeaderlessUnionSelector={itemUnionOptions.length >
                                1}
                            {onChange}
                            {onDelete} />
                    </div>
                {/each}
            </div>
        {/if}
    </div>
{:else}
    <label class="block space-y-1.5">
        {#if showHeader && title}
            <div
                class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div
                    class="flex min-w-0 flex-wrap items-center gap-2 text-xs font-medium tracking-wide text-slate-200">
                    <span class="truncate">{title}</span>
                    {@render renderHeaderDetails(title, description, required)}
                </div>
                {#if anyOfOptions.length > 1}
                    {@render renderUnionSelector(
                        anyOfOptions,
                        currentUnionIndex,
                        pathKey,
                        path,
                        baseSchema,
                        currentUnionValue,
                    )}
                {/if}
            </div>
        {/if}
        {#if showHeaderlessUnionSelector}
            <div class="flex justify-start">
                {@render renderUnionSelector(
                    anyOfOptions,
                    currentUnionIndex,
                    pathKey,
                    path,
                    baseSchema,
                    currentUnionValue,
                )}
            </div>
        {/if}
        {#if isBoolean}
            <label
                class="flex items-center gap-3 rounded-md border border-slate-800/70 bg-slate-950/50 px-3 py-2 text-sm text-slate-100">
                <input
                    type="checkbox"
                    checked={Boolean(value ?? resolvedSchema.default ?? false)}
                    class="h-4 w-4 rounded border-slate-600 bg-slate-900 text-blue-500"
                    onchange={(event) =>
                        onChange(
                            path,
                            (event.currentTarget as HTMLInputElement).checked,
                        )} />
                <span>{title}</span>
            </label>
        {:else if isEnum}
            <select
                class="w-full rounded-md border border-slate-700 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 transition outline-none focus:border-blue-500"
                value={String(value ?? resolvedSchema.default ?? enumOptions[0] ?? "")}
                onchange={(event) =>
                    onChange(path, (event.currentTarget as HTMLSelectElement).value)}>
                {#each enumOptions as option (`${pathKey}:${String(option)}`)}
                    <option value={String(option)}>{String(option)}</option>
                {/each}
            </select>
        {:else if isNumeric}
            <input
                type="number"
                class="w-full rounded-md border border-slate-700 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 transition outline-none focus:border-blue-500"
                value={String(value ?? resolvedSchema.default ?? "")}
                placeholder={resolvedSchema.default !== undefined
                    ? String(resolvedSchema.default)
                    : ""}
                onchange={(event) => {
                    const raw = (event.currentTarget as HTMLInputElement).value;
                    onChange(path, raw === "" ? null : Number(raw));
                }} />
        {:else if isNull}
            <div
                class="rounded-md border border-slate-800/70 bg-slate-950/50 px-3 py-2 text-sm text-slate-300">
                null
            </div>
        {:else}
            <input
                type={resolvedSchema.format === "password" ? "password" : "text"}
                class="w-full rounded-md border border-slate-700 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 transition outline-none focus:border-blue-500"
                value={String(value ?? resolvedSchema.default ?? "")}
                placeholder={resolvedSchema.default !== undefined
                    ? String(resolvedSchema.default)
                    : allowsNull
                      ? "Select None above to clear this field"
                      : ""}
                onchange={(event) =>
                    onChange(path, (event.currentTarget as HTMLInputElement).value)} />
        {/if}
    </label>
{/if}
