/*
 * Portions of the schema traversal in this file are adapted from Arcane
 * (https://github.com/getarcaneapp/arcane), BSD-3-Clause.
 */

import Ajv, { type ValidateFunction } from "ajv";
import { stringify } from "yaml";

export type SchemaObject = Record<string, unknown>;
export type SchemaExtraBehavior = "allow" | "ignore" | "forbid";
export type SchemaPropertyBehavior =
    "defined" | "allowed-extra" | "ignored-extra" | "forbidden-extra" | "unknown";

const EXTRA_BEHAVIOR_KEY = "x-anibridge-extraBehavior";

export type SchemaDoc = {
    title?: string;
    description?: string;
    defaultValue?: string;
    examples?: string[];
    enumValues?: string[];
    type?: string;
};

export type SchemaCompletion = {
    label: string;
    detail?: string;
    documentation?: string;
    apply: string;
    kind: "property" | "enum";
    isSnippet?: boolean;
};

export function asSchemaObject(value: unknown): SchemaObject | null {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return value as SchemaObject;
}

export function createValidator(
    schema: SchemaObject,
): ValidateFunction<unknown> | null {
    try {
        const ajv = new Ajv({
            allErrors: true,
            strict: false,
            strictSchema: false,
            allowUnionTypes: true,
            validateFormats: false,
            validateSchema: false,
        });

        return ajv.compile(schema);
    } catch {
        return null;
    }
}

function getSchemaType(schema: SchemaObject): string | undefined {
    const type = schema["type"];
    if (typeof type === "string") return type;
    if (Array.isArray(type)) {
        for (const entry of type) {
            if (typeof entry === "string") return entry;
        }
    }
    return undefined;
}

function getExtraBehavior(schema: SchemaObject): SchemaExtraBehavior | null {
    const value = schema[EXTRA_BEHAVIOR_KEY];
    if (value === "allow" || value === "ignore" || value === "forbid") {
        return value;
    }
    return null;
}

function formatScalarValue(value: unknown): string {
    if (value === null) return "null";
    if (typeof value === "string") return JSON.stringify(value);
    if (
        typeof value === "number" ||
        typeof value === "boolean" ||
        typeof value === "bigint"
    ) {
        return String(value);
    }
    return JSON.stringify(value);
}

function renderYamlValue(value: unknown): string {
    if (
        value === null ||
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean" ||
        typeof value === "bigint"
    ) {
        return formatScalarValue(value);
    }

    return stringify(value, { indent: 2, lineWidth: 0 }).trimEnd();
}

function indentBlock(value: string, indent = "  "): string {
    return value
        .split("\n")
        .map((line) => `${indent}${line}`)
        .join("\n");
}

function resolveRef(
    root: SchemaObject,
    ref: string,
    visited: Set<string>,
): SchemaObject | null {
    if (!ref.startsWith("#/")) return null;
    if (visited.has(ref)) return null;
    visited.add(ref);

    const segments = ref
        .slice(2)
        .split("/")
        .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));

    let current: unknown = root;
    for (const segment of segments) {
        if (!current || typeof current !== "object") return null;
        current = (current as SchemaObject)[segment];
    }

    return asSchemaObject(current);
}

function expandCandidates(
    root: SchemaObject,
    candidate: SchemaObject,
    visited: Set<string>,
): SchemaObject[] {
    const expanded: SchemaObject[] = [];
    const ref = candidate["$ref"];
    if (typeof ref === "string") {
        const resolved = resolveRef(root, ref, visited);
        if (resolved) {
            expanded.push(...expandCandidates(root, resolved, visited));
        }
    }

    for (const key of ["allOf", "anyOf", "oneOf"]) {
        const node = candidate[key];
        if (!Array.isArray(node)) continue;
        for (const item of node) {
            const asObject = asSchemaObject(item);
            if (asObject) {
                expanded.push(...expandCandidates(root, asObject, visited));
            }
        }
    }

    expanded.push(candidate);

    const unique = new Set<SchemaObject>();
    return expanded.filter((item) => {
        if (unique.has(item)) return false;
        unique.add(item);
        return true;
    });
}

function getPathCandidates(
    root: SchemaObject,
    path: Array<string | number>,
): SchemaObject[] {
    let candidates: SchemaObject[] = [root];

    for (const segment of path) {
        const nextCandidates: SchemaObject[] = [];

        for (const candidate of candidates) {
            const expanded = expandCandidates(root, candidate, new Set<string>());
            for (const node of expanded) {
                if (typeof segment === "number") {
                    const prefixItems = node["prefixItems"];
                    if (Array.isArray(prefixItems) && segment < prefixItems.length) {
                        const fromPrefix = asSchemaObject(prefixItems[segment]);
                        if (fromPrefix) nextCandidates.push(fromPrefix);
                    }

                    const items = asSchemaObject(node["items"]);
                    if (items) nextCandidates.push(items);
                    continue;
                }

                const properties = asSchemaObject(node["properties"]);
                if (properties) {
                    const fromProperty = asSchemaObject(properties[segment]);
                    if (fromProperty) nextCandidates.push(fromProperty);
                }

                const patternProperties = asSchemaObject(node["patternProperties"]);
                if (patternProperties) {
                    for (const patternValue of Object.values(patternProperties)) {
                        const fromPattern = asSchemaObject(patternValue);
                        if (fromPattern) nextCandidates.push(fromPattern);
                    }
                }

                const additionalProperties = asSchemaObject(
                    node["additionalProperties"],
                );
                if (additionalProperties) nextCandidates.push(additionalProperties);
            }
        }

        const unique = new Set<SchemaObject>();
        candidates = nextCandidates.filter((candidate) => {
            if (unique.has(candidate)) return false;
            unique.add(candidate);
            return true;
        });

        if (candidates.length === 0) break;
    }

    return candidates;
}

function matchesPatternProperties(node: SchemaObject, key: string): boolean {
    const patternProperties = asSchemaObject(node["patternProperties"]);
    if (!patternProperties) return false;

    for (const pattern of Object.keys(patternProperties)) {
        try {
            if (new RegExp(pattern).test(key)) {
                return true;
            }
        } catch {
            continue;
        }
    }

    return false;
}

function collectPropertySchemas(
    root: SchemaObject,
    path: Array<string | number>,
): Map<string, SchemaObject> {
    const map = new Map<string, SchemaObject>();
    const candidates = getPathCandidates(root, path);

    for (const candidate of candidates) {
        const expanded = expandCandidates(root, candidate, new Set<string>());
        for (const node of expanded) {
            const properties = asSchemaObject(node["properties"]);
            if (!properties) continue;

            for (const [key, value] of Object.entries(properties)) {
                const propertySchema = asSchemaObject(value);
                if (propertySchema) map.set(key, propertySchema);
            }
        }
    }

    return map;
}

function extractSchemaDoc(schema: SchemaObject): SchemaDoc {
    const title = typeof schema["title"] === "string" ? schema["title"] : undefined;
    const description =
        typeof schema["description"] === "string" ? schema["description"] : undefined;
    const defaultValue =
        schema["default"] !== undefined
            ? formatScalarValue(schema["default"])
            : undefined;
    const examples = Array.isArray(schema["examples"])
        ? schema["examples"].slice(0, 3).map((value) => formatScalarValue(value))
        : undefined;
    const enumValues = Array.isArray(schema["enum"])
        ? schema["enum"].slice(0, 10).map((value) => formatScalarValue(value))
        : undefined;
    const type = getSchemaType(schema);

    return { title, description, defaultValue, examples, enumValues, type };
}

function getSchemaDefaultValue(root: SchemaObject, schema: SchemaObject): unknown {
    const candidates = expandCandidates(root, schema, new Set<string>());

    for (const candidate of candidates) {
        if (candidate["default"] !== undefined) {
            return candidate["default"];
        }
    }

    for (const candidate of candidates) {
        if (candidate["const"] !== undefined) {
            return candidate["const"];
        }
    }

    for (const candidate of candidates) {
        if (Array.isArray(candidate["enum"]) && candidate["enum"].length === 1) {
            return candidate["enum"][0];
        }
    }

    return undefined;
}

function getPropertyInsertText(
    root: SchemaObject,
    key: string,
    schema: SchemaObject,
): { apply: string; isSnippet: boolean } {
    const candidates = expandCandidates(root, schema, new Set<string>());
    const types = new Set<string>();
    let objectLike = false;
    let arrayLike = false;
    const defaultValue = getSchemaDefaultValue(root, schema);

    if (defaultValue !== undefined) {
        const rendered = renderYamlValue(defaultValue);
        if (rendered.includes("\n")) {
            return { apply: `${key}:\n${indentBlock(rendered)}`, isSnippet: false };
        }

        return { apply: `${key}: ${rendered}`, isSnippet: false };
    }

    for (const candidate of candidates) {
        const type = getSchemaType(candidate);
        if (type) types.add(type);

        if (type === "object") objectLike = true;
        if (type === "array") arrayLike = true;

        if (
            candidate["properties"] ||
            candidate["patternProperties"] ||
            asSchemaObject(candidate["additionalProperties"])
        ) {
            objectLike = true;
        }

        if (candidate["items"] || Array.isArray(candidate["prefixItems"])) {
            arrayLike = true;
        }
    }

    if (arrayLike && !objectLike) {
        return { apply: `${key}:\n  - $1`, isSnippet: true };
    }

    if (objectLike) {
        return { apply: `${key}:\n  $1`, isSnippet: true };
    }

    if (types.has("array")) {
        return { apply: `${key}:\n  - $1`, isSnippet: true };
    }

    return { apply: `${key}: `, isSnippet: false };
}

function createPropertyCompletion(
    root: SchemaObject,
    key: string,
    propertySchema: SchemaObject,
): SchemaCompletion {
    const doc = extractSchemaDoc(propertySchema);
    const insert = getPropertyInsertText(root, key, propertySchema);

    return {
        label: key,
        detail: doc.title ?? doc.type,
        documentation: doc.description,
        apply: insert.apply,
        kind: "property",
        isSnippet: insert.isSnippet,
    };
}

function collectValueSuggestions(
    root: SchemaObject,
    path: Array<string | number>,
): Map<string, SchemaCompletion> {
    const suggestions = new Map<string, SchemaCompletion>();
    const candidates = getPathCandidates(root, path);

    for (const candidate of candidates) {
        const expanded = expandCandidates(root, candidate, new Set<string>());
        for (const node of expanded) {
            if (Array.isArray(node["enum"])) {
                for (const value of node["enum"]) {
                    if (
                        typeof value !== "string" &&
                        typeof value !== "number" &&
                        typeof value !== "boolean" &&
                        value !== null
                    ) {
                        continue;
                    }

                    const label = value === null ? "null" : String(value);
                    suggestions.set(label, {
                        label,
                        apply: formatScalarValue(value),
                        kind: "enum",
                        detail: "Enum value",
                    });
                }
            }

            if (
                node["const"] !== undefined &&
                (typeof node["const"] === "string" ||
                    typeof node["const"] === "number" ||
                    typeof node["const"] === "boolean" ||
                    node["const"] === null)
            ) {
                const value = node["const"];
                const label = value === null ? "null" : String(value);
                suggestions.set(label, {
                    label,
                    apply: formatScalarValue(value),
                    kind: "enum",
                    detail: "Constant value",
                });
            }

            const type = getSchemaType(node);
            if (type === "boolean") {
                suggestions.set("true", {
                    label: "true",
                    apply: "true",
                    kind: "enum",
                    detail: "Boolean",
                });
                suggestions.set("false", {
                    label: "false",
                    apply: "false",
                    kind: "enum",
                    detail: "Boolean",
                });
            }

            if (type === "null") {
                suggestions.set("null", {
                    label: "null",
                    apply: "null",
                    kind: "enum",
                    detail: "Null",
                });
            }
        }
    }

    return suggestions;
}

export function getCompletionOptionsForPath(
    schema: SchemaObject | null,
    path: Array<string | number>,
    prefix = "",
): SchemaCompletion[] {
    if (!schema) return [];
    const normalizedPrefix = prefix.toLowerCase();
    const propertyMap = collectPropertySchemas(schema, path);

    return Array.from(propertyMap.entries())
        .filter(([key]) => key.toLowerCase().includes(normalizedPrefix))
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, propertySchema]) =>
            createPropertyCompletion(schema, key, propertySchema),
        );
}

export function getEnumValueCompletions(
    schema: SchemaObject | null,
    path: Array<string | number>,
    prefix = "",
): SchemaCompletion[] {
    if (!schema) return [];

    const normalizedPrefix = prefix.toLowerCase();
    return Array.from(collectValueSuggestions(schema, path).values())
        .filter((entry) => entry.label.toLowerCase().includes(normalizedPrefix))
        .sort((a, b) => a.label.localeCompare(b.label));
}

export function getSchemaDocForPath(
    schema: SchemaObject | null,
    path: Array<string | number>,
): SchemaDoc | null {
    if (!schema) return null;

    const candidates = getPathCandidates(schema, path);
    for (const candidate of candidates) {
        const doc = extractSchemaDoc(candidate);
        if (
            doc.title ||
            doc.description ||
            doc.defaultValue ||
            (doc.examples && doc.examples.length > 0) ||
            (doc.enumValues && doc.enumValues.length > 0) ||
            doc.type
        ) {
            return doc;
        }
    }

    return null;
}

export function getPropertyBehaviorForPath(
    schema: SchemaObject | null,
    path: Array<string | number>,
    key: string,
): SchemaPropertyBehavior {
    if (!schema) return "unknown";

    const candidates = getPathCandidates(schema, path);
    if (candidates.length === 0) return "unknown";

    let supportsDefinedProperty = false;
    let supportsAllowedExtra = false;
    let supportsIgnoredExtra = false;
    let supportsForbiddenExtra = false;

    for (const candidate of candidates) {
        const expanded = expandCandidates(schema, candidate, new Set<string>());
        for (const node of expanded) {
            const properties = asSchemaObject(node["properties"]);
            if (properties && key in properties) {
                supportsDefinedProperty = true;
                continue;
            }

            if (matchesPatternProperties(node, key)) {
                supportsDefinedProperty = true;
                continue;
            }

            const additionalProperties = node["additionalProperties"];
            if (additionalProperties === true || asSchemaObject(additionalProperties)) {
                supportsAllowedExtra = true;
                continue;
            }

            const extraBehavior = getExtraBehavior(node);
            if (extraBehavior === "ignore") {
                supportsIgnoredExtra = true;
                continue;
            }

            if (extraBehavior === "forbid" || additionalProperties === false) {
                supportsForbiddenExtra = true;
            }
        }
    }

    if (supportsDefinedProperty) return "defined";
    if (supportsAllowedExtra) return "allowed-extra";
    if (supportsIgnoredExtra) return "ignored-extra";
    if (supportsForbiddenExtra) return "forbidden-extra";
    return "unknown";
}
