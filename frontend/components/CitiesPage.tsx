"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { Pagination } from "@/components/Pagination";
import { SelectField } from "@/components/SelectField";
import { CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CityRow, type Envelope, formatNumber, formatPercent, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

type GroupedCity = { key: string; name: string; postingCount: number; weightedCount: number; companies: number; share: number };

export function CitiesPage() {
  const [channel, setChannel] = useState("all");
  const [result, setResult] = useState<Envelope<CityRow> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(5);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setResult(await getJson<Envelope<CityRow>>("/api/v1/distributions/cities", channel === "all" ? undefined : { channel })); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取城市数据"); }
    finally { setLoading(false); }
  }, [channel]);
  useEffect(() => { void load(); }, [load]);
  const rows = useMemo(() => groupCities(result?.data ?? []), [result?.data]);
  const chart = useMemo(() => cityOption(rows), [rows]);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const coverage = result?.meta.coverage;
  return (
    <>
      <PageHeader eyebrow="城市分布" title="工作机会集中在哪些城市" description="按岗位地点统计城市热度；一个岗位覆盖多个城市时，份额会在这些城市之间平均分配。" actions={<><SelectField value={channel} options={channelOptions} onValueChange={setChannel} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <div className="content-grid city-grid">
        <Panel className="span-7" title="城市岗位热度" note="图表使用多城市岗位的加权岗位数，避免重复放大">
          {loading ? <LoadingBlock /> : rows.length ? <Chart option={chart} ariaLabel="城市岗位热度排行" className="chart-tall" /> : <EmptyState title="暂无城市分布数据" />}
        </Panel>
        <Panel className="span-5" title="城市排行" note={`数据日期：${coverage?.snapshot_date ?? "暂无"}`}>
          {loading ? <LoadingBlock /> : rows.length ? <><TableWrap><table className="compact-table fit-table"><thead><tr><th>城市</th><th className="numeric">关联岗位</th><th className="numeric">加权份额</th></tr></thead><tbody>{visibleRows.map((row, index) => <tr key={row.key}><td><span className="rank">{(currentPage - 1) * pageSize + index + 1}</span>{row.name}</td><td className="numeric">{formatNumber(row.postingCount)}</td><td className="numeric">{formatPercent(row.share)}</td></tr>)}</tbody></table></TableWrap><Pagination total={rows.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个城市" /></> : <EmptyState title="暂无城市排行" />}
        </Panel>
      </div>
    </>
  );
}

function groupCities(rows: CityRow[]): GroupedCity[] {
  const grouped = new Map<string, GroupedCity>();
  rows.forEach((row) => {
    const key = row.canonical_location_key ?? row.city_name;
    const current = grouped.get(key) ?? { key, name: row.city_name, postingCount: 0, weightedCount: 0, companies: 0, share: 0 };
    current.postingCount += row.posting_count;
    current.weightedCount += Number(row.fractional_posting_count);
    current.companies = Math.max(current.companies, row.covered_company_count ?? 0);
    grouped.set(key, current);
  });
  const total = [...grouped.values()].reduce((sum, row) => sum + row.weightedCount, 0);
  return [...grouped.values()].map((row) => ({ ...row, share: total ? row.weightedCount / total : 0 })).sort((a, b) => b.weightedCount - a.weightedCount);
}

function cityOption(rows: GroupedCity[]): EChartsOption {
  const top = rows.slice(0, 12).reverse();
  return { color: ["#c5554d"], tooltip: { trigger: "axis", axisPointer: { type: "shadow" } }, grid: { left: 84, right: 26, top: 12, bottom: 26 }, xAxis: { type: "value", splitLine: { lineStyle: { color: "#e9eef0" } } }, yAxis: { type: "category", data: top.map((row) => row.name) }, series: [{ type: "bar", barMaxWidth: 22, data: top.map((row) => Number(row.weightedCount.toFixed(2))) }] };
}
