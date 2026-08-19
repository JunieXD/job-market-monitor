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
};

export async function getJson<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const search = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const response = await fetch(`${path}${search.size ? `?${search.toString()}` : ""}`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`API 请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value ?? 0);
}
