export type JsonPath = Array<string | number>;

export type SchemaObject = Record<string, unknown>;

const flexibleJsonOptions: SchemaObject[] = [
    { type: "string" },
    { type: "number" },
    { type: "boolean" },
    { type: "object", additionalProperties: true },
    { type: "array", items: {} },
    { type: "null" },
];

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function schemaMatchesValue(schema: SchemaObject, candidate: unknown): boolean {
    if (candidate === null) return schema.type === "null";
    if (Array.isArray(candidate)) return schema.type === "array";
    if (typeof candidate === "boolean") return schema.type === "boolean";
    if (typeof candidate === "number") {
        return schema.type === "number" || schema.type === "integer";
    }
    if (typeof candidate === "string") return schema.type === "string";
    if (isRecord(candidate)) {
        return schema.type === "object" || isRecord(schema.properties);
    }
    return false;
}

function sortJson(value: unknown): unknown {
    if (Array.isArray(value)) {
        return value.map((entry) => sortJson(entry));
    }

    if (!isRecord(value)) return value;

    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
        const entry = value[key];
        if (entry !== undefined) {
            sorted[key] = sortJson(entry);
        }
    }
    return sorted;
}

function omitKeys(schema: SchemaObject, keys: string[]): SchemaObject {
    return Object.fromEntries(
        Object.entries(schema).filter(([key]) => !keys.includes(key)),
    );
}

function resolveRef(rootSchema: unknown, ref: string): SchemaObject {
    if (!ref.startsWith("#/")) return {};
    let current: unknown = rootSchema;
    for (const segment of ref.slice(2).split("/")) {
        if (!isRecord(current)) return {};
        current = current[segment];
    }
    return isRecord(current) ? current : {};
}

function supportsAnyJsonType(schema: SchemaObject): boolean {
    return (
        typeof schema.type !== "string" &&
        !Array.isArray(schema.anyOf) &&
        !Array.isArray(schema.enum) &&
        !Array.isArray(schema.oneOf) &&
        !Array.isArray(schema.allOf) &&
        !Array.isArray(schema.prefixItems) &&
        schema.const === undefined &&
        !isRecord(schema.properties) &&
        schema.additionalProperties === undefined &&
        schema.items === undefined
    );
}

export function resolveSchema(schema: unknown, rootSchema: unknown): SchemaObject {
    if (!isRecord(schema)) return {};

    let resolved: SchemaObject = { ...schema };
    const seen = new Set<string>();

    while (typeof resolved.$ref === "string") {
        const ref = resolved.$ref;
        if (seen.has(ref)) break;
        seen.add(ref);
        resolved = { ...resolveRef(rootSchema, ref), ...omitKeys(resolved, ["$ref"]) };
    }

    return resolved;
}

export function asRecord(value: unknown): Record<string, unknown> {
    return isRecord(value) ? value : {};
}

export function deepClone<T>(value: T): T {
    return value === undefined ? value : JSON.parse(JSON.stringify(value));
}

export function stableStringify(value: unknown): string {
    return JSON.stringify(sortJson(value));
}

export function humanizeKey(key: string): string {
    const withSpaces = key
        .replace(/_/g, " ")
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .trim();
    return withSpaces ? withSpaces[0].toUpperCase() + withSpaces.slice(1) : key;
}

export function getObjectProperties(
    schema: unknown,
    rootSchema: unknown,
): Record<string, SchemaObject> {
    const resolved = resolveSchema(schema, rootSchema);
    const properties = resolved.properties;
    if (!isRecord(properties)) return {};

    return Object.fromEntries(
        Object.entries(properties).map(([key, value]) => [
            key,
            resolveSchema(value, rootSchema),
        ]),
    );
}

export function getRequiredKeys(schema: unknown, rootSchema: unknown): Set<string> {
    const resolved = resolveSchema(schema, rootSchema);
    return new Set(
        Array.isArray(resolved.required)
            ? resolved.required.filter(
                  (entry): entry is string => typeof entry === "string",
              )
            : [],
    );
}

export function getAdditionalPropertiesSchema(
    schema: unknown,
    rootSchema: unknown,
): SchemaObject | null {
    const resolved = resolveSchema(schema, rootSchema);
    if (resolved.additionalProperties === true) return {};
    return isRecord(resolved.additionalProperties)
        ? resolveSchema(resolved.additionalProperties, rootSchema)
        : null;
}

export function getArrayItemSchema(schema: unknown, rootSchema: unknown): SchemaObject {
    const resolved = resolveSchema(schema, rootSchema);
    return resolveSchema(resolved.items, rootSchema);
}

export function getAnyOfOptions(schema: unknown, rootSchema: unknown): SchemaObject[] {
    const resolved = resolveSchema(schema, rootSchema);
    if (Array.isArray(resolved.anyOf)) {
        return resolved.anyOf.map((option) => resolveSchema(option, rootSchema));
    }

    return supportsAnyJsonType(resolved) ? flexibleJsonOptions : [];
}

export function getPreferredSchema(
    schema: unknown,
    rootSchema: unknown,
    value: unknown,
): SchemaObject {
    const resolved = resolveSchema(schema, rootSchema);
    const options = getAnyOfOptions(resolved, rootSchema);
    if (options.length === 0) return resolved;

    const preferred =
        options.find((option) => schemaMatchesValue(option, value)) ??
        options.find((option) => option.type !== "null");
    return preferred ? { ...omitKeys(resolved, ["anyOf"]), ...preferred } : resolved;
}

export function makeDefaultValue(schema: unknown, rootSchema: unknown): unknown {
    const resolved = getPreferredSchema(schema, rootSchema, undefined);
    if (resolved.default !== undefined) {
        return deepClone(resolved.default);
    }

    if (Array.isArray(resolved.enum) && resolved.enum.length > 0) {
        return deepClone(resolved.enum[0]);
    }

    switch (resolved.type) {
        case "object":
            return {};
        case "array":
            return [];
        case "boolean":
            return false;
        case "integer":
        case "number":
            return 0;
        case "null":
            return null;
        default:
            return "";
    }
}

export function setAtPath(
    root: Record<string, unknown>,
    path: JsonPath,
    value: unknown,
): Record<string, unknown> {
    const clone = deepClone(root ?? {}) as Record<string, unknown>;
    if (path.length === 0) {
        return asRecord(value);
    }

    let current: unknown = clone;
    for (let index = 0; index < path.length - 1; index += 1) {
        const segment = path[index];
        const nextSegment = path[index + 1];

        if (typeof segment === "number") {
            const list = Array.isArray(current) ? current : [];
            list[segment] ??=
                typeof nextSegment === "number" ? [] : ({} as Record<string, unknown>);
            current = list[segment];
            continue;
        }

        const record = asRecord(current);
        const existing = record[segment];
        record[segment] =
            existing ??
            (typeof nextSegment === "number" ? [] : ({} as Record<string, unknown>));
        current = record[segment];
    }

    const last = path[path.length - 1];
    if (typeof last === "number") {
        const list = Array.isArray(current) ? current : [];
        list[last] = value;
        return clone;
    }

    asRecord(current)[last] = value;
    return clone;
}

export function removeAtPath(
    root: Record<string, unknown>,
    path: JsonPath,
): Record<string, unknown> {
    const clone = deepClone(root ?? {}) as Record<string, unknown>;
    if (path.length === 0) return {};

    let current: unknown = clone;
    for (let index = 0; index < path.length - 1; index += 1) {
        const segment = path[index];
        if (typeof segment === "number") {
            if (!Array.isArray(current)) return clone;
            current = current[segment];
            continue;
        }
        if (!isRecord(current)) return clone;
        current = current[segment];
    }

    const last = path[path.length - 1];
    if (typeof last === "number") {
        if (Array.isArray(current)) {
            current.splice(last, 1);
        }
        return clone;
    }

    if (isRecord(current)) {
        delete current[last];
    }
    return clone;
}
