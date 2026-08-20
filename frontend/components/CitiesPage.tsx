"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { MultiSelectFilter } from "@/components/MultiSelectFilter";
import { Pagination } from "@/components/Pagination";
import { SelectField } from "@/components/SelectField";
import { CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CityRow, type CompanyMeta, type Envelope, formatNumber, formatPercent, getCachedJson, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

type GroupedCity = { key: string; name: string; postingCount: number; companies: number; share: number };

export function CitiesPage() {
  const [channel, setChannel] = useState("all");
  const [companyKeys, setCompanyKeys] = useState<string[] | null>(null);
  const [companies, setCompanies] = useState<CompanyMeta[]>(() => getCachedJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies")?.data ?? []);
  const [result, setResult] = useState<Envelope<CityRow> | null>(() => getCachedJson("/api/v1/distributions/cities"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(5);
  const load = useCallback(async () => {
    const params = {
      channel: channel === "all" ? undefined : channel,
      company_keys: companyKeys ?? undefined,
    };
    const cachedCompanies = getCachedJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies");
    const cachedResult = getCachedJson<Envelope<CityRow>>("/api/v1/distributions/cities", params);
    if (cachedCompanies) setCompanies(cachedCompanies.data);
    if (cachedResult) setResult(cachedResult);
    setLoading(!cachedResult); setError(null);
    try {
      const [companyResult, cityResult] = await Promise.all([
        getJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies"),
        getJson<Envelope<CityRow>>("/api/v1/distributions/cities", params),
      ]);
      setCompanies(companyResult.data);
      setResult(cityResult);
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取城市数据"); }
    finally { setLoading(false); }
  }, [channel, companyKeys]);
  useEffect(() => { void load(); }, [load]);
  const rows = useMemo(() => groupCities(result?.data ?? []), [result?.data]);
  const companyOptions = useMemo(() => companies.map((item) => ({ value: item.key, label: item.name, companyKey: item.key })), [companies]);
  const chart = useMemo(() => cityOption(rows), [rows]);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const coverage = result?.meta.coverage;
  const topFiveCoverage = rows.slice(0, 5).reduce((sum, row) => sum + row.share, 0);
  const selectedCompanyCount = companyKeys?.length ?? companies.length;
  return (
    <>
      <PageHeader eyebrow="城市分布" title="工作机会城市分布" description="比较公司与招聘类型在各城市的岗位数量。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <div className="city-toolbar">
        <MultiSelectFilter label="公司" options={companyOptions} values={companyKeys} onValuesChange={(values) => { setCompanyKeys(values); setPage(1); }} ariaLabel="筛选公司" minimumSelected={1} />
        <SelectField value={channel} options={channelOptions} onValueChange={(value) => { setChannel(value); setPage(1); }} ariaLabel="选择招聘类型" />
      </div>
      <div className="city-insights" aria-label="城市分布摘要">
        <div><span>覆盖城市</span><strong>{formatNumber(rows.length)}</strong></div>
        <div><span>岗位最多</span><strong>{rows[0]?.name ?? "-"}</strong></div>
        <div><span>前五覆盖率之和</span><strong>{formatPercent(topFiveCoverage)}</strong></div>
        <div><span>已选公司</span><strong>{formatNumber(selectedCompanyCount)}</strong></div>
      </div>
      <div className="content-grid city-grid">
        <Panel className="span-7 city-panel" title="城市岗位热度" note="岗位涉及该城市即完整计 1">
          {loading && !result ? <LoadingBlock /> : rows.length ? <Chart option={chart} ariaLabel="城市岗位热度排行" className="chart-tall" /> : <EmptyState title="暂无城市分布数据" />}
        </Panel>
        <Panel className="span-5 city-panel" title="城市排行" note={coverage?.snapshot_date ? `更新至 ${coverage.snapshot_date}` : "暂无数据"}>
          {loading && !result ? <LoadingBlock /> : rows.length ? <><TableWrap><table className="compact-table fit-table city-table"><thead><tr><th>城市</th><th>岗位</th><th>公司</th><th>岗位覆盖率</th></tr></thead><tbody>{visibleRows.map((row, index) => <tr key={row.key}><td><span className="rank">{(currentPage - 1) * pageSize + index + 1}</span>{row.name}</td><td>{formatNumber(row.postingCount)}</td><td>{formatNumber(row.companies)}</td><td>{formatPercent(row.share)}</td></tr>)}</tbody></table></TableWrap><Pagination total={rows.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个城市" /></> : <EmptyState title="暂无城市排行" />}
        </Panel>
      </div>
    </>
  );
}

function groupCities(rows: CityRow[]): GroupedCity[] {
  const grouped = new Map<string, GroupedCity>();
  const companySets = new Map<string, Set<string>>();
  const totalPostingCount = Math.max(...rows.map((row) => Number(row.total_posting_count ?? 0)), 0)
    || rows.reduce((sum, row) => sum + Number(row.fractional_posting_count), 0);
  rows.forEach((row) => {
    const key = row.canonical_location_key ?? row.city_name;
    const current = grouped.get(key) ?? { key, name: row.city_name, postingCount: 0, companies: 0, share: 0 };
    current.postingCount += row.posting_count;
    const companySet = companySets.get(key) ?? new Set<string>();
    if (row.company_key) companySet.add(row.company_key);
    companySets.set(key, companySet);
    current.companies = Math.max(current.companies, row.covered_company_count ?? companySet.size);
    grouped.set(key, current);
  });
  return [...grouped.values()]
    .map((row) => ({ ...row, share: totalPostingCount ? row.postingCount / totalPostingCount : 0 }))
    .sort((a, b) => b.postingCount - a.postingCount || a.name.localeCompare(b.name, "zh-CN"));
}

function cityOption(rows: GroupedCity[]): EChartsOption {
  const top = rows.slice(0, 12).reverse();
  return { color: ["#c5554d"], tooltip: { trigger: "axis", axisPointer: { type: "shadow" } }, grid: { left: 84, right: 26, top: 12, bottom: 26 }, xAxis: { type: "value", minInterval: 1, splitLine: { lineStyle: { color: "#e9eef0" } } }, yAxis: { type: "category", data: top.map((row) => row.name) }, series: [{ type: "bar", barMaxWidth: 22, data: top.map((row) => row.postingCount) }] };
}
