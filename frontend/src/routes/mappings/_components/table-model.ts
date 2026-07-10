import type { Mapping, MappingEdge } from "$lib/types/api";
import type { ColumnConfig } from "./columns";

export const AUTHORITY_COLUMN_PREFIX = "authority:";

export interface MappingSourceCell {
    value: string;
    scope: string | null;
    query: string;
}

export interface MappingTargetCell {
    edge: MappingEdge;
    key: string;
    rangeLabel: string;
    query: string;
}

export type MappingTableCell =
    | { kind: "title"; column: ColumnConfig }
    | { kind: "sources"; column: ColumnConfig }
    | { kind: "actions"; column: ColumnConfig }
    | {
          kind: "authority";
          column: ColumnConfig;
          authority: string;
          source: MappingSourceCell | null;
          targets: MappingTargetCell[];
      }
    | { kind: "unknown"; column: ColumnConfig };

export interface MappingTableRow {
    key: string;
    mapping: Mapping;
    sourcesKey: string;
    sourceCount: number;
    cells: MappingTableCell[];
}

export function authorityFromColumn(columnId: string): string | null {
    return columnId.startsWith(AUTHORITY_COLUMN_PREFIX)
        ? columnId.slice(AUTHORITY_COLUMN_PREFIX.length)
        : null;
}

export function formatDestinationRange(value: string | null | undefined): string {
    if (value === null || value === undefined) return "null";
    return value === "" ? '""' : value;
}

export function edgeKey(edge: MappingEdge): string {
    return `${edge.target_authority}:${edge.target_value}:${edge.target_scope ?? ""}:${edge.source_range}:${formatDestinationRange(edge.destination_range)}`;
}

export function sourceMappingQuery(authority: string, value: string): string {
    return `source.authority:${authority} source.value:${value}`;
}

export function targetMappingQuery(edge: MappingEdge): string {
    const terms = [
        `target.authority:${edge.target_authority}`,
        `target.value:${edge.target_value}`,
    ];
    if (edge.target_scope) terms.push(`target.scope:${edge.target_scope}`);
    return terms.join(" ");
}

function groupTargetsByAuthority(mapping: Mapping): Map<string, MappingTargetCell[]> {
    const grouped = new Map<string, MappingTargetCell[]>();
    for (const edge of mapping.edges ?? []) {
        const targets = grouped.get(edge.target_authority) ?? [];
        targets.push({
            edge,
            key: edgeKey(edge),
            rangeLabel: `${edge.source_range} → ${formatDestinationRange(edge.destination_range)}`,
            query: targetMappingQuery(edge),
        });
        grouped.set(edge.target_authority, targets);
    }
    return grouped;
}

function buildAuthorityCell(
    mapping: Mapping,
    column: ColumnConfig,
    authority: string,
    targetsByAuthority: Map<string, MappingTargetCell[]>,
): MappingTableCell {
    return {
        kind: "authority",
        column,
        authority,
        source:
            mapping.authority === authority
                ? {
                      value: mapping.value,
                      scope: mapping.scope,
                      query: sourceMappingQuery(authority, mapping.value),
                  }
                : null,
        targets: targetsByAuthority.get(authority) ?? [],
    };
}

export function buildMappingTableRows(
    items: Mapping[],
    visibleColumns: ColumnConfig[],
): MappingTableRow[] {
    return items.map((mapping) => {
        const targetsByAuthority = groupTargetsByAuthority(mapping);
        const cells = visibleColumns.map((column): MappingTableCell => {
            if (column.id === "title") return { kind: "title", column };
            if (column.id === "sources") return { kind: "sources", column };
            if (column.id === "actions") return { kind: "actions", column };

            const authority = authorityFromColumn(column.id);
            if (authority) {
                return buildAuthorityCell(
                    mapping,
                    column,
                    authority,
                    targetsByAuthority,
                );
            }

            return { kind: "unknown", column };
        });

        const sources = mapping.sources ?? [];
        return {
            key: mapping.descriptor,
            mapping,
            sourcesKey: `${sources.join("|")}:${String(mapping.custom)}`,
            sourceCount: sources.length,
            cells,
        };
    });
}
