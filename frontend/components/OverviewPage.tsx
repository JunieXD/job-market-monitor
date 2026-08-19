"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import { BriefcaseBusiness, ShieldCheck, TrendingDown, TrendingUp } from "lucide-react";

import { Chart } from "@/components/Chart";
import { CompanyName } from "@/components/CompanyLogo";
import { Pagination } from "@/components/Pagination";
import { SelectField } from "@/components/SelectField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, MetricCard, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyRow, type Coverage, type Envelope, formatNumber, formatPercent, getCachedJson, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

type OverviewData = { overview: Envelope<CompanyRow>; trend: Envelope<CompanyRow> };

export function OverviewPage() {
  const [channel, setChannel] = useState("all");
  const [data, setData] = useState<OverviewData | null>(() => {
    const overview = getCachedJson<Envelope<CompanyRow>>("/api/v1/overview");
    const trend = getCachedJson<Envelope<CompanyRow>>("/api/v1/trends/market");
    return overview && trend ? { overview, trend } : null;
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(5);

  const load = useCallback(async () => {
    const params = channel === "all" ? undefined : { channel };
    const cachedOverview = getCachedJson<Envelope<CompanyRow>>("/api/v1/overview", params);
    const cachedTrend = getCachedJson<Envelope<CompanyRow>>("/api/v1/trends/market", params);
    if (cachedOverview && cachedTrend) setData({ overview: cachedOverview, trend: cachedTrend });
    setLoading(!(cachedOverview && cachedTrend));
    setError(null);
    try {
      const [overview, trend] = await Promise.all([
        getJson<Envelope<CompanyRow>>("/api/v1/overview", params),
        getJson<Envelope<CompanyRow>>("/api/v1/trends/market", params),
      ]);
      setData({ overview, trend });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "暂时无法读取市场数据");
    } finally {
      setLoading(false);
    }
  }, [channel]);

  useEffect(() => { void load(); }, [load]);

  const rows = data?.overview.data ?? [];
  const coverage = data?.overview.meta.coverage ?? emptyCoverage;
  const active = rows.reduce((sum, row) => sum + row.active_posting_count, 0);
  const added = rows.reduce((sum, row) => sum + row.new_posting_count, 0);
  const closed = rows.reduce((sum, row) => sum + row.closed_posting_count, 0);
  const companies = new Set(rows.map((row) => row.company_key)).size;
  const trendOption = useMemo(() => marketTrendOption(data?.trend.data ?? []), [data?.trend.data]);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <>
      <PageHeader
        eyebrow="市场总览"
        title="就业市场概况"
        description="企业官方招聘岗位的规模与每日变化。"
        actions={<><SelectField value={channel} options={channelOptions} onValueChange={(value) => { setChannel(value); setPage(1); }} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>}
      />
      {error && <ErrorNotice message={error} />}
      {!loading && !error && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <section className="metric-grid" aria-label="市场核心指标">
        <MetricCard icon={<BriefcaseBusiness size={17} />} label="在招岗位" value={formatNumber(active)} detail={`${formatNumber(companies)} 家公司`} />
        <MetricCard icon={<TrendingUp size={17} />} label="较上一日新增" value={formatNumber(added)} detail="按最新采集日统计" tone="amber" />
        <MetricCard icon={<TrendingDown size={17} />} label="较上一日关闭" value={formatNumber(closed)} detail="按最新采集日统计" tone="coral" />
        <MetricCard icon={<ShieldCheck size={17} />} label="数据覆盖率" value={formatPercent(coverage.coverage_ratio)} detail={`${coverage.standard_snapshot_count} / ${coverage.configured_source_channel_count} 个招聘渠道`} tone="blue" />
      </section>
      <div className="content-grid">
        <Panel className="span-8" title="岗位数量变化趋势" note={coverage.snapshot_date ? `更新至 ${coverage.snapshot_date}` : undefined}>
          {loading && !data ? <LoadingBlock /> : data?.trend.data.length ? <Chart option={trendOption} ariaLabel="岗位规模、新增和关闭趋势" /> : <EmptyState title="暂无趋势数据" />}
        </Panel>
        <Panel className="span-4" title="今日数据采集情况" note={coverage.snapshot_date ? `更新至 ${coverage.snapshot_date}` : "暂无数据"}>
          {loading && !data ? <LoadingBlock /> : rows.length ? <><CompanyCoverageTable rows={visibleRows} /><Pagination total={rows.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="条采集记录" /></> : <EmptyState title="今日暂无采集数据" />}
        </Panel>
      </div>
    </>
  );
}

function CompanyCoverageTable({ rows }: { rows: CompanyRow[] }) {
  return (
    <TableWrap><table className="compact-table fit-table overview-table"><thead><tr><th>公司</th><th>招聘类型</th><th>岗位</th></tr></thead><tbody>
      {rows.map((row) => <tr key={`${row.source_id}-${row.channel}`}><td><CompanyName companyKey={row.company_key} companyName={row.company_name} /></td><td><ChannelTag channel={row.channel} /></td><td className="numeric">{formatNumber(row.active_posting_count)}</td></tr>)}
    </tbody></table></TableWrap>
  );
}

function marketTrendOption(rows: CompanyRow[]): EChartsOption {
  const grouped = new Map<string, { active: number; added: number; closed: number }>();
  rows.forEach((row) => {
    const value = grouped.get(row.snapshot_date) ?? { active: 0, added: 0, closed: 0 };
    value.active += row.active_posting_count;
    value.added += row.new_posting_count;
    value.closed += row.closed_posting_count;
    grouped.set(row.snapshot_date, value);
  });
  const dates = [...grouped.keys()].sort();
  return {
    color: ["#137a70", "#b06a22", "#c5554d"],
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, data: ["在招岗位", "新增", "关闭"] },
    grid: { left: 48, right: 18, top: 20, bottom: 48 },
    xAxis: { type: "category", data: dates, axisLabel: { color: "#66727c" } },
    yAxis: { type: "value", axisLabel: { color: "#66727c" }, splitLine: { lineStyle: { color: "#e9eef0" } } },
    series: [
      { name: "在招岗位", type: "line", smooth: true, symbolSize: 7, data: dates.map((day) => grouped.get(day)?.active ?? 0) },
      { name: "新增", type: "bar", barMaxWidth: 18, data: dates.map((day) => grouped.get(day)?.added ?? 0) },
      { name: "关闭", type: "bar", barMaxWidth: 18, data: dates.map((day) => grouped.get(day)?.closed ?? 0) },
    ],
  };
}

const emptyCoverage: Coverage = {
  snapshot_date: null,
  configured_source_channel_count: 0,
  standard_snapshot_count: 0,
  successful_source_channel_count: 0,
  absence_authoritative_source_channel_count: 0,
  non_authoritative_successful_run_count: 0,
  failed_run_count: 0,
  coverage_ratio: 0,
};
