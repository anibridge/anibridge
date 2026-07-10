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

export interface RefStepPayload {
    axis: string;
    value: string | number;
}

export interface RefPayload {
    key: string;
    path?: RefStepPayload[];
}

export interface RecordSnapshotValue {
    state?: { native?: string | null; status?: string | null };
    progress?: { current?: number | null; total?: number | null; unit?: string | null };
    rating?: { value: number; scale: [number, number, number] };
    scalar?: string | number | boolean;
    date_value?: string;
    datetime_value?: string;
}

export interface RecordSnapshot {
    ref: RefPayload;
    kind?: string;
    surface?: string;
    key?: string | null;
    ids?: string[];
    values?: Record<string, RecordSnapshotValue>;
}

// --- Mappings API ---
export interface MappingEdge {
    target_authority: string;
    target_value: string;
    target_scope: string | null;
    source_range: string;
    destination_range?: string | null;
    sources?: string[];
}

export interface Mapping {
    descriptor: string;
    authority: string;
    value: string;
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
    authority: string;
    value: string;
    scope?: string | null;
    ranges: RangeInputPayload[];
    deleted?: boolean;
}

export interface MappingOverridePayload {
    descriptor: string;
    targets: TargetPayload[];
}

export interface MappingConfigPayload {
    includes: string[];
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
    authority: string;
    value: string;
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
    authority: string;
    value: string;
    scope: string | null;
    layers: MappingLayers;
    targets: MappingTarget[];
}

export interface MappingConfig {
    mappings_url: string;
    includes: string[];
    path: string;
    format: string;
}

export interface ListMappingsResponse {
    items: Mapping[];
    total: number;
    page: number;
    per_page: number;
    pages: number;
    with_anilist: boolean;
}

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
    source_namespace: string;
    target_namespace: string;
    source_account?: string | null;
    target_account?: string | null;
    scan_interval?: number | string | null;
    poll_interval?: number | string | null;
    scan_modes?: string[];
    full_scan?: boolean | null;
    destructive_sync?: boolean | null;
}

export interface CurrentSync {
    state: "running" | "idle" | string;
    started_at: string;
    stage: string;
    source_namespace: string;
    target_namespace: string;
    trigger: string;
    scanned_items: number;
    processed_items: number;
    total_items?: number | null;
}

export interface ProfileRuntimeStatus {
    running: boolean;
    last_synced?: string | null;
    current_sync?: CurrentSync | null;
    initialization_error?: string | null;
    scheduler?: Record<string, unknown> | null;
}

export interface ProfileStatus {
    config: ProfileConfig;
    status: ProfileRuntimeStatus;
}

export interface StatusResponse {
    profiles: Record<string, ProfileStatus>;
    scheduler?: Record<string, unknown> | null;
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
    settings?: Record<string, unknown> | null;
    settings_error?: string | null;
}

export interface ConfigDocumentUpdateRequest {
    content: string;
    expected_mtime?: number | null;
}

export interface ConfigStructuredUpdateRequest {
    settings: Record<string, unknown>;
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

export interface RestartResponse {
    ok: boolean;
    message: string;
}

// --- History API ---
export interface HistoryOperation {
    id: number;
    group_id: number;
    profile_name: string;
    resource_kind: string;
    action: string;
    outcome: string;
    timestamp: string;
    source_namespace?: string | null;
    source_ref?: RefPayload | null;
    target_namespace?: string | null;
    target_ref?: RefPayload | null;
    source_surface?: string | null;
    target_surface?: string | null;
    resource_key?: string | null;
    before_state?: RecordSnapshot | null;
    after_state?: RecordSnapshot | null;
    info?: Record<string, string> | null;
    error_message?: string | null;
    ephemeral?: boolean;
    pinned?: boolean;
}

export interface HistoryGroup {
    id: number;
    run_id: number;
    profile_name: string;
    outcome: string;
    timestamp: string;
    source_namespace?: string | null;
    source_parent_ref?: RefPayload | null;
    target_namespace?: string | null;
    target_parent_ref?: RefPayload | null;
    animap_authority?: string | null;
    animap_value?: string | null;
    animap_scope?: string | null;
    operation_count?: number;
    record_count?: number;
    event_count?: number;
    node_count?: number;
    error_count?: number;
    info?: Record<string, string> | null;
    ephemeral?: boolean;
    source_media?: ProviderMediaMetadata | null;
    target_media?: ProviderMediaMetadata | null;
    operations?: HistoryOperation[];
}

export interface GetHistoryResponse {
    groups: HistoryGroup[];
    limit: number;
    has_more: boolean;
    next_before_id?: number | null;
    latest_group_id?: number | null;
    stats?: Record<string, number> | null;
    resource_stats?: Record<string, number> | null;
}

export interface PinResponse {
    profile_name: string;
    target_namespace: string;
    target_parent_ref: RefPayload;
    created_at: string;
    updated_at: string;
    media?: ProviderMediaMetadata | null;
}

export interface PinListResponse {
    pins: PinResponse[];
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
