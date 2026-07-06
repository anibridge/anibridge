<script lang="ts">
    import { onDestroy, onMount } from "svelte";

    import type { ValidateFunction } from "ajv";
    import type * as Monaco from "monaco-editor";

    import {
        analyzeYamlSource,
        buildSchemaDiagnostics,
        diagnosticsToMarkers,
        findIgnoredExtraRanges,
    } from "./yaml-analysis";
    import { asSchemaObject, createValidator, type SchemaObject } from "./yaml-schema";

    type Props = {
        value?: string;
        readOnly?: boolean;
        fontSize?: string;
        fileUri?: string;
        autoHeight?: boolean;
        schemaObject?: unknown;
        schemaUrl?: string;
        theme?: "light" | "dark";
    };

    let {
        value = $bindable(""),
        readOnly = false,
        fontSize = "13px",
        fileUri = undefined,
        autoHeight = false,
        schemaObject = undefined,
        schemaUrl = undefined,
        theme = "dark",
    }: Props = $props();

    const langId = "yaml";
    const markerOwner = "yaml-editor";

    let monacoModule: typeof import("./monaco") | null = null;
    let monacoInstance = $state<typeof import("./monaco").monaco | null>(null);
    let editorElement: HTMLDivElement;
    let editor = $state<Monaco.editor.IStandaloneCodeEditor | null>(null);
    let model = $state<Monaco.editor.ITextModel | null>(null);
    let ownsModel = false;
    let resizeObserver: ResizeObserver | null = null;
    let changeDisposable: Monaco.IDisposable | null = null;
    let autoHeightDispose: (() => void) | null = null;
    let validationMarkers = $state<Monaco.editor.IMarkerData[]>([]);
    let ignoredDecorationIds: string[] = [];
    let resolvedSchema = $state<SchemaObject | null>(null);
    let schemaValidator = $state<ValidateFunction<unknown> | null>(null);
    let schemaFetchAbort: AbortController | null = null;

    const editorTheme = $derived(
        theme === "dark" ? "catppuccin-mocha" : "catppuccin-latte",
    );
    const fontSizeValue = $derived(
        Number.parseInt(fontSize.replace("px", ""), 10) || 13,
    );

    function updateHeight() {
        if (!editor || !editorElement || !autoHeight) return;
        editorElement.style.height = `${editor.getContentHeight()}px`;
        editor.layout();
    }

    onMount(async () => {
        if (!editorElement) return;

        monacoModule = await import("./monaco");
        const monaco = monacoModule.monaco;
        monacoInstance = monaco;

        await monacoModule.initShiki(monaco);
        await new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
        );

        const uri = fileUri
            ? monaco.Uri.parse(fileUri)
            : monaco.Uri.parse(`inmemory://model-${Date.now()}.${langId}`);
        const existingModel = monaco.editor.getModel(uri);
        ownsModel = !existingModel;
        model = existingModel || monaco.editor.createModel(value, langId, uri);
        const activeModel = model;

        const createdEditor = monaco.editor.create(editorElement, {
            model: activeModel,
            automaticLayout: false,
            theme: editorTheme,
            readOnly,
            fontSize: fontSizeValue,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            wordWrap: "on",
            fixedOverflowWidgets: true,
            dragAndDrop: false,
            contextmenu: true,
            quickSuggestions: { other: true, comments: false, strings: true },
            suggestOnTriggerCharacters: true,
            wordBasedSuggestions: "off",
            suggest: { showWords: false },
            fontFamily:
                'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", "Courier New", monospace',
            fontLigatures: false,
            padding: { top: 10, bottom: 10 },
            scrollbar: autoHeight
                ? { vertical: "hidden", handleMouseWheel: false }
                : { vertical: "auto", handleMouseWheel: true },
        });

        editor = createdEditor;

        createdEditor.addAction({
            id: "mre.selectAll",
            label: "Select All",
            contextMenuGroupId: "9_cutcopypaste",
            contextMenuOrder: 4,
            run: (activeEditor: Monaco.editor.IStandaloneCodeEditor) => {
                activeEditor.focus();
                const activeEditorModel = activeEditor.getModel();
                if (activeEditorModel) {
                    activeEditor.setSelection(activeEditorModel.getFullModelRange());
                }
            },
        });

        changeDisposable = activeModel.onDidChangeContent(() => {
            value = activeModel.getValue() || "";
        });

        resizeObserver = new ResizeObserver(() => {
            requestAnimationFrame(() => {
                editor?.layout();
            });
        });
        resizeObserver.observe(editorElement);
    });

    onDestroy(() => {
        changeDisposable?.dispose();
        resizeObserver?.disconnect();
        autoHeightDispose?.();

        if (monacoModule && model) {
            monacoModule.clearYamlSchemaForModel(model.uri);
        }
        if (monacoInstance && model) {
            monacoInstance.editor.setModelMarkers(model, markerOwner, []);
        }
        if (editor && ignoredDecorationIds.length > 0) {
            editor.deltaDecorations(ignoredDecorationIds, []);
        }

        editor?.dispose();
        if (ownsModel) model?.dispose();
    });

    $effect(() => {
        if (schemaFetchAbort) {
            schemaFetchAbort.abort();
            schemaFetchAbort = null;
        }

        const directSchema = asSchemaObject(schemaObject);
        if (directSchema) {
            resolvedSchema = directSchema;
            schemaValidator = createValidator(directSchema);
            return;
        }

        if (!schemaUrl) {
            resolvedSchema = null;
            schemaValidator = null;
            return;
        }

        const controller = new AbortController();
        schemaFetchAbort = controller;
        resolvedSchema = null;
        schemaValidator = null;

        (async () => {
            try {
                const response = await fetch(schemaUrl, {
                    cache: "no-store",
                    signal: controller.signal,
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);

                const payload = asSchemaObject(await response.json());
                if (controller.signal.aborted) return;

                resolvedSchema = payload;
                schemaValidator = payload ? createValidator(payload) : null;
            } catch {
                if (controller.signal.aborted) return;
                resolvedSchema = null;
                schemaValidator = null;
            }
        })();

        return () => {
            controller.abort();
            if (schemaFetchAbort === controller) {
                schemaFetchAbort = null;
            }
        };
    });

    $effect(() => {
        const activeModel = model;
        const api = monacoModule;
        if (!activeModel || !api) return;

        api.setYamlSchemaForModel(activeModel.uri, resolvedSchema);
        return () => {
            api.clearYamlSchemaForModel(activeModel.uri);
        };
    });

    $effect(() => {
        const activeEditor = editor;
        const activeModel = model;
        const monaco = monacoInstance;
        if (!activeModel || !monaco) {
            validationMarkers = [];
            return;
        }

        const analysis = analyzeYamlSource(value);
        const markers = diagnosticsToMarkers(monaco, activeModel, analysis.diagnostics);
        const ignoredRanges = findIgnoredExtraRanges(value, resolvedSchema);

        markers.push(
            ...diagnosticsToMarkers(
                monaco,
                activeModel,
                ignoredRanges.map((range) => ({
                    from: range.from,
                    to: range.to,
                    severity: "hint",
                    message: range.message,
                })),
            ),
        );

        if (
            schemaValidator &&
            !analysis.diagnostics.some((diagnostic) => diagnostic.severity === "error")
        ) {
            try {
                const parsedValue = analysis.doc.toJS();
                const valid = schemaValidator(parsedValue);
                if (!valid) {
                    markers.push(
                        ...diagnosticsToMarkers(
                            monaco,
                            activeModel,
                            buildSchemaDiagnostics(
                                schemaValidator.errors,
                                analysis.doc,
                                value,
                            ),
                        ),
                    );
                }
            } catch {
                // Syntax diagnostics already cover parser failures.
            }
        }

        validationMarkers = markers;

        if (!activeEditor) return;

        ignoredDecorationIds = activeEditor.deltaDecorations(
            ignoredDecorationIds,
            ignoredRanges.map((range) => {
                const start = activeModel.getPositionAt(range.from);
                const end = activeModel.getPositionAt(
                    Math.max(range.from + 1, range.to),
                );

                return {
                    range: new monaco.Range(
                        start.lineNumber,
                        start.column,
                        end.lineNumber,
                        end.column,
                    ),
                    options: {
                        inlineClassName: "yaml-ignored-config",
                        hoverMessage: { value: range.message },
                        stickiness:
                            monaco.editor.TrackedRangeStickiness
                                .NeverGrowsWhenTypingAtEdges,
                    },
                };
            }),
        );
    });

    $effect(() => {
        if (model && value !== model.getValue()) {
            model.setValue(value);
        }
    });

    $effect(() => {
        if (model && monacoInstance) {
            monacoInstance.editor.setModelLanguage(model, langId);
        }
    });

    $effect(() => {
        if (model && monacoInstance) {
            monacoInstance.editor.setModelMarkers(
                model,
                markerOwner,
                validationMarkers,
            );
        }
    });

    $effect(() => {
        if (editor) {
            editor.updateOptions({
                readOnly,
                fontSize: fontSizeValue,
                wordWrap: "on",
                fixedOverflowWidgets: true,
                dragAndDrop: false,
                wordBasedSuggestions: "off",
                suggest: { showWords: false },
                scrollbar: autoHeight
                    ? { vertical: "hidden", handleMouseWheel: false }
                    : { vertical: "auto", handleMouseWheel: true },
            });
            editor.layout();
        }
    });

    $effect(() => {
        if (editor && monacoInstance) {
            monacoInstance.editor.setTheme(editorTheme);
        }
    });

    $effect(() => {
        if (autoHeightDispose) {
            autoHeightDispose();
            autoHeightDispose = null;
        }

        if (editor && autoHeight) {
            const disposable = editor.onDidContentSizeChange(updateHeight);
            updateHeight();
            autoHeightDispose = () => disposable.dispose();
        } else if (editorElement) {
            editorElement.style.height = "";
        }
    });
</script>

<div
    class={`editor-root ${autoHeight ? "auto" : ""}`}
    bind:this={editorElement}>
</div>

<style>
    .editor-root {
        width: 100%;
        height: 100%;
        min-height: 0;
        background: transparent;
    }

    .editor-root.auto {
        height: auto;
    }

    .editor-root :global(.monaco-editor) {
        background: transparent;
    }

    .editor-root :global(.monaco-editor-background) {
        background: transparent;
    }

    .editor-root :global(.monaco-editor .margin) {
        background: transparent;
    }

    .editor-root :global(.yaml-ignored-config) {
        opacity: 0.5;
        filter: saturate(0.65);
    }
</style>
