import type * as Monaco from "monaco-editor/esm/vs/editor/editor.api.js";

import { findYamlPositionContext } from "./yaml-analysis";
import {
    getCompletionOptionsForPath,
    getEnumValueCompletions,
    getSchemaDocForPath,
    type SchemaCompletion,
    type SchemaDoc,
    type SchemaObject,
} from "./yaml-schema";

const yamlSchemasByModel = new Map<string, SchemaObject | null>();

function getModelKey(uri: Monaco.Uri | string): string {
    return typeof uri === "string" ? uri : uri.toString();
}

function getSchemaForModel(model: Monaco.editor.ITextModel): SchemaObject | null {
    return yamlSchemasByModel.get(getModelKey(model.uri)) ?? null;
}

function escapeMarkdown(value: string): string {
    return value.replace(/[\\`*_{}[\]()#+\-.!|>]/g, "\\$&");
}

function getLinePrefix(
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
): string {
    return model.getLineContent(position.lineNumber).slice(0, position.column - 1);
}

function getCurrentToken(
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
    pattern = /[\w.-]*$/,
) {
    const linePrefix = getLinePrefix(model, position);
    const match = linePrefix.match(pattern);
    const text = match?.[0] ?? "";
    const startColumn = position.column - text.length;

    return {
        text,
        range: {
            startLineNumber: position.lineNumber,
            endLineNumber: position.lineNumber,
            startColumn,
            endColumn: position.column,
        },
    };
}

function isLikelyKeyPosition(
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
): boolean {
    const linePrefix = getLinePrefix(model, position);
    return /^\s*(?:-\s*)?[\w.-]*$/.test(linePrefix);
}

function getSequenceItemPath(
    context: NonNullable<ReturnType<typeof findYamlPositionContext>>,
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
): Array<string | number> | null {
    const linePrefix = getLinePrefix(model, position);
    if (!/^\s*-\s*[\w.-]*$/.test(linePrefix)) return null;

    const pathTail = context.path[context.path.length - 1];
    if (typeof pathTail === "number") return context.path;

    const parentTail = context.parentPath[context.parentPath.length - 1];
    if (typeof parentTail === "number") return context.parentPath;

    return [...context.path, 0];
}

function formatInsertText(
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
    completion: SchemaCompletion,
): string {
    if (!completion.isSnippet || !completion.apply.includes("\n")) {
        return completion.apply;
    }

    const indent = model.getLineContent(position.lineNumber).match(/^\s*/)?.[0];

    return completion.apply.replace(/\n {2}/g, `\n${indent ?? ""}  `);
}

function formatDocumentation(
    doc: SchemaDoc,
    fallbackTitle?: string,
): string | undefined {
    const lines: string[] = [];
    const title = doc.title ?? fallbackTitle;
    if (title) {
        lines.push(`**${escapeMarkdown(title)}**`);
    }
    if (doc.description) {
        lines.push(escapeMarkdown(doc.description));
    }
    if (doc.type) {
        lines.push(`Type: \`${escapeMarkdown(doc.type)}\``);
    }
    if (doc.defaultValue) {
        lines.push(`Default: \`${escapeMarkdown(doc.defaultValue)}\``);
    }
    if (doc.enumValues && doc.enumValues.length > 0) {
        lines.push(
            `Allowed: ${doc.enumValues
                .map((value) => `\`${escapeMarkdown(value)}\``)
                .join(", ")}`,
        );
    }
    if (doc.examples && doc.examples.length > 0) {
        lines.push(
            `Examples: ${doc.examples
                .map((value) => `\`${escapeMarkdown(value)}\``)
                .join(", ")}`,
        );
    }

    return lines.length > 0 ? lines.join("\n\n") : undefined;
}

function toCompletionItem(
    monaco: typeof Monaco,
    model: Monaco.editor.ITextModel,
    position: Monaco.Position,
    range: Monaco.IRange,
    completion: SchemaCompletion,
): Monaco.languages.CompletionItem {
    return {
        label: completion.label,
        detail: completion.detail,
        documentation: completion.documentation,
        kind:
            completion.kind === "property"
                ? monaco.languages.CompletionItemKind.Property
                : monaco.languages.CompletionItemKind.EnumMember,
        insertText: formatInsertText(model, position, completion),
        insertTextRules: completion.isSnippet
            ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
            : undefined,
        sortText:
            completion.kind === "property"
                ? `0-${completion.label}`
                : `1-${completion.label}`,
        range,
    };
}

function getPropertyPath(context: ReturnType<typeof findYamlPositionContext>) {
    if (!context) return [];
    return context.atKey ? context.parentPath : context.path;
}

export function registerYamlCompletionProvider(monaco: typeof Monaco) {
    return monaco.languages.registerCompletionItemProvider("yaml", {
        triggerCharacters: [" ", ":", "-", ".", '"', "'"],
        provideCompletionItems: (model, position) => {
            const schema = getSchemaForModel(model);
            if (!schema) return { suggestions: [] };

            const source = model.getValue();
            const offset = model.getOffsetAt(position);
            const yamlContext = findYamlPositionContext(source, offset);
            if (!yamlContext) return { suggestions: [] };

            const token = getCurrentToken(model, position);
            const isKeyPosition =
                yamlContext.atKey || isLikelyKeyPosition(model, position);
            const sequenceItemPath = getSequenceItemPath(yamlContext, model, position);

            const completions = sequenceItemPath
                ? (() => {
                      const itemPropertyCompletions = getCompletionOptionsForPath(
                          schema,
                          sequenceItemPath,
                          token.text,
                      );
                      return itemPropertyCompletions.length > 0
                          ? itemPropertyCompletions
                          : getEnumValueCompletions(
                                schema,
                                sequenceItemPath,
                                token.text,
                            );
                  })()
                : isKeyPosition
                  ? getCompletionOptionsForPath(
                        schema,
                        getPropertyPath(yamlContext),
                        token.text,
                    )
                  : getEnumValueCompletions(schema, yamlContext.path, token.text);

            return {
                suggestions: completions.map((completion) =>
                    toCompletionItem(monaco, model, position, token.range, completion),
                ),
            };
        },
    });
}

export function registerYamlHoverProvider(monaco: typeof Monaco) {
    return monaco.languages.registerHoverProvider("yaml", {
        provideHover: (model, position) => {
            const schema = getSchemaForModel(model);
            if (!schema) return null;

            const source = model.getValue();
            const offset = model.getOffsetAt(position);
            const yamlContext = findYamlPositionContext(source, offset);
            if (!yamlContext || !yamlContext.currentKey) return null;

            const doc = getSchemaDocForPath(schema, yamlContext.path);
            if (!doc) return null;

            const contents = formatDocumentation(doc, yamlContext.currentKey);
            if (!contents) return null;

            const startOffset = yamlContext.keyFrom ?? offset;
            const endOffset = yamlContext.keyTo ?? Math.min(source.length, offset + 1);
            const start = model.getPositionAt(startOffset);
            const end = model.getPositionAt(endOffset);

            return {
                range: new monaco.Range(
                    start.lineNumber,
                    start.column,
                    end.lineNumber,
                    end.column,
                ),
                contents: [{ value: contents }],
            };
        },
    });
}

export function registerYamlProviders(monaco: typeof Monaco) {
    return [registerYamlCompletionProvider(monaco), registerYamlHoverProvider(monaco)];
}

export function setYamlSchemaForModel(
    uri: Monaco.Uri | string,
    schema: SchemaObject | null,
) {
    yamlSchemasByModel.set(getModelKey(uri), schema);
}

export function clearYamlSchemaForModel(uri: Monaco.Uri | string) {
    yamlSchemasByModel.delete(getModelKey(uri));
}
