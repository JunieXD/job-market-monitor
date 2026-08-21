export type Coverage = {
  snapshot_date: string | null;
  configured_source_channel_count: number;
  standard_snapshot_count: number;
  successful_source_channel_count: number;
  absence_authoritative_source_channel_count: number;
  non_authoritative_successful_run_count: number;
  failed_run_count: number;
  coverage_ratio: number;
};

export type AnalyticsMeta = {
  snapshot_date: string | null;
  timezone: string;
  filters: Record<string, unknown>;
  coverage: Coverage;
  metric_definition: string;
  pagination?: { limit: number; offset: number; total: number };
};

export type Envelope<T> = { data: T[]; meta: AnalyticsMeta };

export type CompanyRow = {
  snapshot_date: string;
  company_key: string;
  company_name: string;
  channel: string;
  source_id: number;
  active_posting_count: number;
  new_posting_count: number;
  changed_posting_count: number;
  first_missing_posting_count: number;
  closed_posting_count: number;
  reopened_posting_count: number;
};

export type CategoryRow = {
  canonical_category_key?: string | null;
  canonical_category_name?: string | null;
  canonical_category_id?: number | null;
  source_category_name?: string;
  category_status?: string;
  category_assignment_method?: string | null;
  posting_count: number;
  market_category_share?: number;
  source_category_share?: number;
};

export type CityRow = {
  city_name: string;
  company_key?: string;
  company_name?: string;
  canonical_location_key?: string | null;
  posting_count: number;
  total_posting_count?: number;
  posting_share?: number;
  fractional_posting_count: number;
  fractional_share: number;
  covered_company_count?: number;
};

export type JobRow = {
  external_id: string;
  source_key: string;
  company_key: string;
  company_name: string;
  channel: string;
  title: string;
  source_url: string;
  published_at: string | null;
  source_updated_at: string | null;
  status: string;
  recruitment_count: number | null;
  first_seen_at: string;
  last_seen_at: string;
};

export type JobDetail = JobRow & {
  source_name: string;
  employment_type_name: string;
  recruitment_project_name: string | null;
  description: string | null;
  requirements: string | null;
  source_status: string | null;
  degree_code: string | null;
  degree_name: string | null;
  experience_min_years: number | null;
  experience_max_years: number | null;
  graduation_start_at: string | null;
  graduation_end_at: string | null;
  department_code: string | null;
  department_name: string | null;
  interview_location_names: string[];
  last_changed_at: string;
  closed_at: string | null;
  locations: Array<{
    code: string;
    name: string;
    source_name: string;
    country_name: string | null;
    state_name: string | null;
    district_name: string | null;
    address: string | null;
  }>;
  categories: Array<{
    external_id: string;
    name: string;
    parent_name: string | null;
    assignment_method: string;
  }>;
  business_units: Array<{ code: string; name: string }>;
  version_count: number;
};

export type CompanyMeta = {
  key: string;
  name: string;
  source_count: number;
};

export type CollectionChannel = {
  source_key: string;
  display_name: string;
  company_key: string;
  company_name: string;
  channel: string;
  run_id: string | null;
  attempt_status: string | null;
  state: "completed" | "running" | "failed" | "partial" | "pending";
  started_at: string | null;
  finished_at: string | null;
  discovered_count: number | null;
  page_count: number | null;
  complete: boolean | null;
  absence_authoritative: boolean | null;
  is_standard: boolean;
  last_standard_date: string | null;
  error_summary: string | null;
};

export type CollectionStatus = {
  snapshot_date: string;
  timezone: string;
  checked_at: string;
  schedule: {
    frequency: "daily";
    hour: number;
    minute: number;
    next_run_at: string;
  };
  summary: {
    total: number;
    completed: number;
    running: number;
    failed: number;
    partial: number;
    pending: number;
    progress_ratio: number;
    started_at: string | null;
    last_activity_at: string | null;
  };
  channels: CollectionChannel[];
};

export async function getJson<T>(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): Promise<T> {
  const url = buildUrl(path, params);
  const pending = pendingRequests.get(url) as Promise<T> | undefined;
  if (pending) return pending;
  const request = fetch(url, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`API 请求失败（${response.status}）`);
      const payload = await response.json() as T;
      const previous = responseCache.get(url) as T | undefined;
      const stablePayload = previous !== undefined && jsonEqual(previous, payload)
        ? previous
        : payload;
      responseCache.set(url, stablePayload);
      persistJson(url, stablePayload);
      return stablePayload;
    })
    .finally(() => pendingRequests.delete(url));
  pendingRequests.set(url, request as Promise<unknown>);
  return request;
}

export function getCachedJson<T>(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): T | null {
  return (responseCache.get(buildUrl(path, params)) as T | undefined) ?? null;
}

export function getStoredJson<T>(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): T | null {
  const url = buildUrl(path, params);
  const memoryValue = responseCache.get(url) as T | undefined;
  if (memoryValue !== undefined) return memoryValue;
  const stored = readPersistentCache();
  const entry = stored[url];
  if (!entry) return null;
  if (
    typeof entry !== "object"
    || typeof entry.savedAt !== "number"
    || !("payload" in entry)
    || Date.now() - entry.savedAt > maxCacheAge(url)
  ) {
    delete stored[url];
    writePersistentCache(stored);
    return null;
  }
  responseCache.set(url, entry.payload);
  return entry.payload as T;
}

export async function prefetchJson(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): Promise<void> {
  if (getStoredJson(path, params) !== null) return;
  await getJson(path, params).then(() => undefined).catch(() => undefined);
}

function buildUrl(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): string {
  const search = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (Array.isArray(value)) {
      value.forEach((item) => search.append(key, item));
    } else if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  return `${path}${search.size ? `?${search.toString()}` : ""}`;
}

const responseCache = new Map<string, unknown>();
const pendingRequests = new Map<string, Promise<unknown>>();
const persistentCacheKey = "job-market-monitor:api-cache:v1";
const persistentCacheLimit = 32;
const analyticsCacheMaxAgeMs = 24 * 60 * 60 * 1_000;
const collectionCacheMaxAgeMs = 30_000;

type PersistentCache = Record<string, { savedAt: number; payload: unknown }>;

function maxCacheAge(url: string): number {
  return url.startsWith("/api/v1/collection/status")
    ? collectionCacheMaxAgeMs
    : analyticsCacheMaxAgeMs;
}

function readPersistentCache(): PersistentCache {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(persistentCacheKey);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as PersistentCache;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    clearPersistentCache();
    return {};
  }
}

function writePersistentCache(cache: PersistentCache): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(persistentCacheKey, JSON.stringify(cache));
  } catch {
    clearPersistentCache();
  }
}

function clearPersistentCache(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(persistentCacheKey);
  } catch {
    return;
  }
}

function persistJson(url: string, payload: unknown): void {
  const stored = readPersistentCache();
  stored[url] = { savedAt: Date.now(), payload };
  const overflow = Object.entries(stored)
    .sort(([, left], [, right]) => right.savedAt - left.savedAt)
    .slice(persistentCacheLimit);
  overflow.forEach(([key]) => { delete stored[key]; });
  writePersistentCache(stored);
}

function jsonEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

export function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value ?? 0);
}

export function formatPercent(value: number | undefined): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value ?? 0);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
