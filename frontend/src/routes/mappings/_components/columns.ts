export interface ColumnConfig {
    id: string;
    title: string;
    visible: boolean;
    width: number;
    minWidth: number;
    resizable: boolean;
}
export const STATIC_COLUMNS: ColumnConfig[] = [
    {
        id: "title",
        title: "Title",
        visible: true,
        width: 200,
        minWidth: 100,
        resizable: true,
    },
    {
        id: "sources",
        title: "Sources",
        visible: true,
        width: 100,
        minWidth: 100,
        resizable: false,
    },
    {
        id: "actions",
        title: "Actions",
        visible: true,
        width: 100,
        minWidth: 100,
        resizable: false,
    },
];

export const COLUMNS_STORAGE_KEY = "mappings.columns.v5";
