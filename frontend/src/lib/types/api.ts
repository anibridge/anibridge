import type { Media as AniListMedia } from "$lib/types/anilist";

// --- Generic ---
export type ApiResult<T> = Promise<T>;

export interface OkResponse {
    ok: boolean;
}

export interface ProviderMediaMetadata {
    namespace: string;
    key: string;
    title?: string | null;
    poster_url?: string | null;
    external_url?: string | null;
    labels?: string[] | null;
}

// --- Mappings API ---
export interface MappingEdge {
    target_provider: string;
    target_entry_id: string;
    target_scope: string | null;
    source_range: string;
    destination_range?: string | null;
    sources?: string[];
}

export interface Mapping {
    descriptor: string;
    provider: string;
    entry_id: string;
    scope: string | null;
    edges: MappingEdge[];
    custom?: boolean;
    sources?: string[];
    anilist?: AniListMedia | null;
}

export interface RangeInputPayload {
    source_range: string;
    destination_range: string | null;
}

export interface TargetPayload {
    provider: string;
    entry_id: string;
    scope?: string | null;
    ranges: RangeInputPayload[];
    deleted?: boolean;
}

export interface MappingOverridePayload {
    descriptor: string;
    targets: TargetPayload[];
}

export type RangeOrigin = "upstream" | "custom";
export type TargetOrigin = "upstream" | "custom" | "mixed";

export interface MappingRangeView {
    source_range: string;
    upstream?: string | null;
    custom?: string | null;
    effective?: string | null;
    origin: RangeOrigin;
    inherited?: boolean;
}

export interface MappingTarget {
    descriptor: string;
    provider: string;
    entry_id: string;
    scope: string | null;
    origin: TargetOrigin;
    deleted?: boolean;
    ranges: MappingRangeView[];
}

export interface MappingLayers {
    upstream: Record<string, Record<string, string | null> | null>;
    custom: Record<string, Record<string, string | null> | null>;
    effective: Record<string, Record<string, string | null> | null>;
}

export interface MappingDetail {
    descriptor: string;
    provider: string;
    entry_id: string;
    scope: string | null;
    layers: MappingLayers;
    targets: MappingTarget[];
}

export interface ListMappingsResponse {
    items: Mapping[];
    total: number;
    page: number;
    per_page: number;
    pages: number;
    with_anilist: boolean;
}

export type DeleteMappingResponse = OkResponse;

export type FieldType = "int" | "string" | "enum";
export type FieldOperator = "=" | ">" | ">=" | "<" | "<=" | "*" | "?" | "range" | "in";

export interface FieldCapability {
    key: string;
    aliases: string[];
    type: FieldType;
    operators: FieldOperator[];
    values?: string[] | null;
    desc?: string | null;
}

export interface QueryCapabilitiesResponse {
    fields: FieldCapability[];
}

// --- Logs API ---
export interface LogFile {
    name: string;
    size: number;
    mtime: number;
    current: boolean;
}

export interface LogEntry {
    timestamp: string | null;
    level: string;
    message: string;
}

// --- Status / System API ---
export interface ProfileConfig {
    library_namespace?: string;
    list_namespace?: string;
    library_user?: string | null;
    list_user?: string | null;
    scan_interval?: number | null;
    poll_interval?: number | null;
    scan_modes?: string[];
    full_scan?: boolean | null;
    destructive_sync?: boolean | null;
    batch_requests?: boolean | null;
}

export interface CurrentSync {
    state?: string;
    started_at?: string;
    section_index?: number;
    section_count?: number;
    section_title?: string | null;
    stage?: string;
    section_items_total?: number;
    section_items_processed?: number;
}

export interface ProfileRuntimeStatus {
    running: boolean;
    last_synced?: string | null;
    current_sync?: CurrentSync | null;
}

export interface ProfileStatus {
    config: ProfileConfig;
    status: ProfileRuntimeStatus;
}

export interface StatusResponse {
    profiles: Record<string, ProfileStatus>;
}

export interface SettingsProfile {
    name: string;
    settings: Record<string, unknown>;
}

export interface SettingsResponse {
    global_config: Record<string, unknown>;
    profiles: SettingsProfile[];
}

export interface ConfigDocumentResponse {
    config_path: string;
    file_exists: boolean;
    content: string;
    mtime?: number | null;
    schema?: Record<string, unknown> | null;
}

export interface ConfigDocumentUpdateRequest {
    content: string;
    expected_mtime?: number | null;
}

export interface ConfigUpdateResponse {
    ok: boolean;
    profiles: string[];
    requires_restart: boolean;
    mtime?: number | null;
}

export interface AboutInfo {
    version: string;
    git_hash: string;
    python: string;
    platform: string;
    utc_now: string;
    started_at?: string | null;
    uptime_seconds?: number | null;
    uptime?: string | null;
    sqlite?: string | null;
}

export interface ProcessInfo {
    pid: number;
    cpu_count?: number | null;
    memory_mb?: number | null;
}

export interface SchedulerSummary {
    running: boolean;
    configured_profiles: number;
    total_profiles: number;
    running_profiles: number;
    syncing_profiles: number;
    sync_mode_counts: Record<string, number>;
    most_recent_sync?: string | null;
    most_recent_sync_profile?: string | null;
    next_database_sync_at?: string | null;
    profiles: Record<string, ProfileStatus>;
}

export interface AboutResponse {
    info: AboutInfo;
    process: ProcessInfo;
    scheduler: SchedulerSummary;
    status: Record<string, ProfileStatus>;
}

export interface MetaResponse {
    version: string;
    git_hash: string;
}

// --- History API ---
export interface HistoryItem {
    id: number;
    profile_name: string;
    library_namespace?: string | null;
    library_section_key?: string | null;
    library_media_key?: string | null;
    list_namespace?: string | null;
    list_media_key?: string | null;
    animap_entry_id?: number | null;
    media_kind?: string | null;
    outcome: string;
    before_state?: Record<string, unknown> | null;
    after_state?: Record<string, unknown> | null;
    info?: Record<string, string> | null;
    error_message?: string | null;
    timestamp: string;
    library_media?: ProviderMediaMetadata | null;
    list_media?: ProviderMediaMetadata | null;
    pinned_fields?: string[] | null;
}

export interface GetHistoryResponse {
    items: HistoryItem[];
    page: number;
    per_page: number;
    total: number;
    pages: number;
    stats: Record<string, number>;
}

export interface UndoResponse {
    item: HistoryItem;
}

export interface RetryResponse {
    ok: boolean;
}

export interface PinFieldOption {
    value: string;
    label: string;
}

export interface PinResponse {
    profile_name: string;
    list_namespace: string;
    list_media_key: string;
    fields: string[];
    created_at: string;
    updated_at: string;
    media?: ProviderMediaMetadata | null;
}

export interface PinListResponse {
    pins: PinResponse[];
}

export interface PinOptionsResponse {
    options: PinFieldOption[];
}

export interface PinSearchResult {
    media: ProviderMediaMetadata;
    pin?: PinResponse | null;
}

export interface PinSearchResponse {
    results: PinSearchResult[];
}

// --- Backups API ---
export interface BackupMeta {
    filename: string;
    created_at: string;
    size_bytes: number;
    entries?: number | null;
    user?: string | null;
    age_seconds: number;
}

export interface ListBackupsResponse {
    backups: BackupMeta[];
}

export interface RawBackup {
    [key: string]: unknown;
}
