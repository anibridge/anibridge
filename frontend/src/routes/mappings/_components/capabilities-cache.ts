import { apiJson } from "$lib/utils/api";

export type OperatorToken = "=" | ">" | ">=" | "<" | "<=" | "*" | "?" | "in" | "range";

export type FieldCapability = {
    key: string;
    aliases?: string[];
    type: "int" | "string" | "enum" | string;
    operators: OperatorToken[];
    values?: string[] | null;
    desc?: string | null;
};

type CapabilitiesResponse = { fields: FieldCapability[] };

let cachedCapabilities: FieldCapability[] | null | undefined;
let inFlight: Promise<FieldCapability[] | null> | null = null;

export async function loadCapabilities(): Promise<FieldCapability[] | null> {
    if (cachedCapabilities !== undefined) {
        return cachedCapabilities;
    }

    if (!inFlight) {
        inFlight = apiJson<CapabilitiesResponse>("/api/mappings/query-capabilities")
            .then((res) => {
                if (res && Array.isArray(res.fields)) {
                    cachedCapabilities = res.fields as FieldCapability[];
                } else {
                    cachedCapabilities = null;
                }
                return cachedCapabilities;
            })
            .catch((error) => {
                cachedCapabilities = undefined;
                inFlight = null;
                throw error;
            });
    }

    return inFlight;
}

export function clearCapabilitiesCache(): void {
    cachedCapabilities = undefined;
    inFlight = null;
}
