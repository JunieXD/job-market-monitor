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
      responseCache.set(url, payload);
      return payload;
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

export async function prefetchJson(
  path: string,
  params?: Record<string, string | number | string[] | null | undefined>,
): Promise<void> {
  if (getCachedJson(path, params) !== null) return;
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
