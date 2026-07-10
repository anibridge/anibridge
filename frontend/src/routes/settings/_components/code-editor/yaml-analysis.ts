/*
 * Portions of the YAML position analysis in this file are adapted from Arcane
 * (https://github.com/getarcaneapp/arcane), BSD-3-Clause.
 */

import type { ErrorObject } from "ajv";
import type * as Monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import {
    isMap,
    isPair,
    isScalar,
    isSeq,
    parseDocument,
    type ParsedNode,
    type Scalar,
} from "yaml";

import { getPropertyBehaviorForPath, type SchemaObject } from "./yaml-schema";

const TAB_INDENT_REGEX = /(^|\n)(\t+)/g;

type YamlDocLike = {
    getIn: (path: Array<string | number>, keepScalar?: boolean) => unknown;
    contents?: ParsedNode | null;
    errors: Array<{ message?: string; pos?: [number, number] }>;
    toJS: () => unknown;
};

export type YamlPositionContext = {
    path: Array<string | number>;
    parentPath: Array<string | number>;
    currentKey?: string;
    atKey: boolean;
    keyFrom?: number;
    keyTo?: number;
};

export type YamlDiagnostic = {
    from: number;
    to: number;
    severity: "error" | "warning" | "info" | "hint";
    message: string;
};

export type YamlAnalysisResult = { doc: YamlDocLike; diagnostics: YamlDiagnostic[] };

export type IgnoredYamlRange = { from: number; to: number; message: string };

function decodePointerSegment(segment: string): string {
    return segment.replace(/~1/g, "/").replace(/~0/g, "~");
}

function pointerToPath(pointer: string): Array<string | number> {
    if (!pointer) return [];

    return pointer
        .split("/")
        .slice(1)
        .filter((segment) => segment.length > 0)
        .map((segment) => {
            const decoded = decodePointerSegment(segment);
            return /^\d+$/.test(decoded) ? Number(decoded) : decoded;
        });
}

function getRange(node: unknown): [number, number] | null {
    if (!node || typeof node !== "object") return null;
    const range = (node as { range?: [number, number, number] }).range;
    if (!range || range.length < 2) return null;
    return [range[0], range[1]];
}

function containsPosition(node: unknown, position: number): boolean {
    const range = getRange(node);
    if (!range) return false;
    return position >= range[0] && position <= range[1];
}

function scalarToKey(value: unknown): string | null {
    if (!isScalar(value)) return null;
    const scalarValue = (value as Scalar<unknown>).value;
    if (typeof scalarValue === "string" || typeof scalarValue === "number") {
        return String(scalarValue);
    }
    return null;
}

function findContextInNode(
    node: ParsedNode | null | undefined,
    position: number,
    path: Array<string | number>,
): YamlPositionContext | null {
    if (!node) return null;
    if (!containsPosition(node, position)) return null;

    if (isMap(node)) {
        for (const pair of node.items) {
            if (!isPair(pair)) continue;

            const key = scalarToKey(pair.key);
            const keyRange = getRange(pair.key);
            if (key && keyRange && position >= keyRange[0] && position <= keyRange[1]) {
                return {
                    path: [...path, key],
                    parentPath: [...path],
                    currentKey: key,
                    atKey: true,
                    keyFrom: keyRange[0],
                    keyTo: keyRange[1],
                };
            }

            if (key && pair.value && containsPosition(pair.value, position)) {
                const nested = findContextInNode(pair.value as ParsedNode, position, [
                    ...path,
                    key,
                ]);
                if (nested) return nested;

                return {
                    path: [...path, key],
                    parentPath: [...path],
                    currentKey: key,
                    atKey: false,
                    keyFrom: keyRange?.[0],
                    keyTo: keyRange?.[1],
                };
            }
        }

        return { path: [...path], parentPath: [...path], atKey: false };
    }

    if (isSeq(node)) {
        for (let index = 0; index < node.items.length; index += 1) {
            const item = node.items[index] as ParsedNode | null;
            if (!item || !containsPosition(item, position)) continue;

            const nested = findContextInNode(item, position, [...path, index]);
            if (nested) return nested;

            return { path: [...path, index], parentPath: [...path], atKey: false };
        }
    }

    return { path: [...path], parentPath: path.slice(0, -1), atKey: false };
}

function getPairRange(pair: unknown, key: unknown): [number, number] | null {
    return getRange(pair) ?? getRange(key);
}

function createDocument(source: string): YamlDocLike {
    return parseDocument(source, {
        strict: true,
        uniqueKeys: false,
        merge: true,
    }) as unknown as YamlDocLike;
}

function getNodeRangeByPath(
    doc: YamlDocLike,
    path: Array<string | number>,
): { from: number; to: number } | null {
    const node = doc.getIn(path, true) as { range?: [number, number, number] } | null;
    const range = node?.range;
    if (!range || range.length < 2) return null;

    return { from: range[0], to: Math.max(range[0] + 1, range[1]) };
}

function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function findKeyRangeInSource(
    source: string,
    key: string,
): { from: number; to: number } | null {
    const keyRegex = new RegExp(`^\\s*${escapeRegExp(key)}\\s*:`, "m");
    const match = keyRegex.exec(source);
    if (!match || match.index < 0) return null;

    return {
        from: match.index,
        to: Math.min(source.length, match.index + Math.max(1, key.length)),
    };
}

function collectDuplicateKeyDiagnostics(
    node: ParsedNode | null | undefined,
    diagnostics: YamlDiagnostic[],
): number {
    if (!node) return 0;
    let duplicateCount = 0;

    if (isMap(node)) {
        const seen = new Set<string>();

        for (const item of node.items) {
            if (!isPair(item)) continue;
            const key = scalarToKey(item.key);
            if (key) {
                const keyRange = getRange(item.key);
                if (seen.has(key) && key !== "<<") {
                    duplicateCount += 1;
                    diagnostics.push({
                        from: keyRange?.[0] ?? 0,
                        to: Math.max((keyRange?.[0] ?? 0) + 1, keyRange?.[1] ?? 1),
                        severity: "error",
                        message: `Duplicate YAML key "${key}"`,
                    });
                }

                seen.add(key);
            }

            duplicateCount += collectDuplicateKeyDiagnostics(
                item.value as ParsedNode | null,
                diagnostics,
            );
        }
    }

    if (isSeq(node)) {
        for (const item of node.items) {
            duplicateCount += collectDuplicateKeyDiagnostics(
                item as ParsedNode | null,
                diagnostics,
            );
        }
    }

    return duplicateCount;
}

function buildTabDiagnostics(source: string): YamlDiagnostic[] {
    const diagnostics: YamlDiagnostic[] = [];

    for (const match of source.matchAll(TAB_INDENT_REGEX)) {
        const tabs = match[2] || "";
        const newlineLength = match[1] === "\n" ? 1 : 0;
        const start = (match.index ?? 0) + newlineLength;

        diagnostics.push({
            from: start,
            to: Math.max(start + 1, start + tabs.length),
            severity: "error",
            message: "Tabs are not allowed for YAML indentation. Use spaces only.",
        });
    }

    return diagnostics;
}

export function findYamlPositionContext(
    source: string,
    position: number,
): YamlPositionContext | null {
    const doc = createDocument(source);
    return findContextInNode((doc.contents as ParsedNode | null) ?? null, position, []);
}

export function analyzeYamlSource(source: string): YamlAnalysisResult {
    const doc = createDocument(source);
    const diagnostics: YamlDiagnostic[] = [];

    diagnostics.push(...buildTabDiagnostics(source));

    for (const error of doc.errors) {
        const start = error.pos?.[0] ?? 0;
        const end = error.pos?.[1] ?? Math.min(source.length, start + 1);

        diagnostics.push({
            from: start,
            to: Math.max(start + 1, end),
            severity: "error",
            message: error.message || "YAML syntax error",
        });
    }

    collectDuplicateKeyDiagnostics(doc.contents ?? null, diagnostics);

    return { doc, diagnostics };
}

export function buildSchemaDiagnostics(
    errors: ErrorObject[] | null | undefined,
    doc: YamlDocLike,
    source: string,
): YamlDiagnostic[] {
    if (!errors || errors.length === 0) return [];

    const diagnostics: YamlDiagnostic[] = [];

    for (const error of errors) {
        const path = pointerToPath(error.instancePath || "");
        const params = error.params as Record<string, unknown>;
        const missingProperty =
            typeof params["missingProperty"] === "string"
                ? params["missingProperty"]
                : null;
        const additionalProperty =
            typeof params["additionalProperty"] === "string"
                ? params["additionalProperty"]
                : null;

        if (error.keyword === "additionalProperties" && additionalProperty === "<<") {
            continue;
        }

        const range = getNodeRangeByPath(doc, path) ||
            (missingProperty ? findKeyRangeInSource(source, missingProperty) : null) ||
            (additionalProperty
                ? findKeyRangeInSource(source, additionalProperty)
                : null) || { from: 0, to: Math.min(source.length, 1) };

        let message = `${error.instancePath || "/"} ${error.message || "is invalid"}`;
        if (error.keyword === "required" && missingProperty) {
            message = `Missing required property "${missingProperty}"`;
        }
        if (error.keyword === "additionalProperties" && additionalProperty) {
            message = `Unsupported property "${additionalProperty}"`;
        }

        diagnostics.push({
            from: range.from,
            to: range.to,
            severity: "error",
            message,
        });
    }

    return diagnostics;
}

function collectIgnoredExtraRangesInNode(
    node: ParsedNode | null | undefined,
    path: Array<string | number>,
    schema: SchemaObject,
    ranges: IgnoredYamlRange[],
) {
    if (!node) return;

    if (isMap(node)) {
        for (const item of node.items) {
            if (!isPair(item)) continue;

            const key = scalarToKey(item.key);
            if (!key) continue;

            const propertyBehavior = getPropertyBehaviorForPath(schema, path, key);
            if (propertyBehavior === "ignored-extra") {
                const pairRange = getPairRange(item, item.key);
                if (pairRange) {
                    ranges.push({
                        from: pairRange[0],
                        to: Math.max(pairRange[0] + 1, pairRange[1]),
                        message: `"${key}" is ignored by the config model and has no effect.`,
                    });
                }
                continue;
            }

            if (item.value) {
                collectIgnoredExtraRangesInNode(
                    item.value as ParsedNode,
                    [...path, key],
                    schema,
                    ranges,
                );
            }
        }
        return;
    }

    if (isSeq(node)) {
        for (let index = 0; index < node.items.length; index += 1) {
            collectIgnoredExtraRangesInNode(
                node.items[index] as ParsedNode | null,
                [...path, index],
                schema,
                ranges,
            );
        }
    }
}

export function findIgnoredExtraRanges(
    source: string,
    schema: SchemaObject | null,
): IgnoredYamlRange[] {
    if (!schema) return [];

    const doc = createDocument(source);
    const ranges: IgnoredYamlRange[] = [];
    collectIgnoredExtraRangesInNode(
        (doc.contents as ParsedNode | null) ?? null,
        [],
        schema,
        ranges,
    );
    return ranges;
}

function toMarkerSeverity(
    monaco: typeof Monaco,
    severity: YamlDiagnostic["severity"],
): Monaco.MarkerSeverity {
    switch (severity) {
        case "warning":
            return monaco.MarkerSeverity.Warning;
        case "info":
            return monaco.MarkerSeverity.Info;
        case "hint":
            return monaco.MarkerSeverity.Hint;
        default:
            return monaco.MarkerSeverity.Error;
    }
}

export function diagnosticsToMarkers(
    monaco: typeof Monaco,
    model: Monaco.editor.ITextModel,
    diagnostics: YamlDiagnostic[],
): Monaco.editor.IMarkerData[] {
    const maxOffset = model.getValueLength();

    return diagnostics.map((diagnostic) => {
        const startOffset = Math.max(0, Math.min(diagnostic.from, maxOffset));
        const endOffset = Math.max(
            Math.min(maxOffset, startOffset + 1),
            Math.min(diagnostic.to, maxOffset),
        );
        const start = model.getPositionAt(startOffset);
        const end = model.getPositionAt(endOffset);

        return {
            severity: toMarkerSeverity(monaco, diagnostic.severity),
            message: diagnostic.message,
            startLineNumber: start.lineNumber,
            startColumn: start.column,
            endLineNumber: end.lineNumber,
            endColumn: end.column,
        };
    });
}
