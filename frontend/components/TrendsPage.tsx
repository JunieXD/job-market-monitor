"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { CompanyName } from "@/components/CompanyLogo";
import { Pagination } from "@/components/Pagination";
import { SelectField } from "@/components/SelectField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyRow, type Envelope, formatNumber, getCachedJson, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

export function TrendsPage() {
  const [channel, setChannel] = useState("all");
  const [result, setResult] = useState<Envelope<CompanyRow> | null>(() => getCachedJson("/api/v1/trends/companies"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const load = useCallback(async () => {
    const params = channel === "all" ? undefined : { channel };
    const cached = getCachedJson<Envelope<CompanyRow>>("/api/v1/trends/companies", params);
    if (cached) setResult(cached);
    setLoading(!cached); setError(null);
    try {
      setResult(await getJson<Envelope<CompanyRow>>("/api/v1/trends/companies", params));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取趋势数据"); }
    finally { setLoading(false); }
  }, [channel]);
  useEffect(() => { void load(); }, [load]);

  const rows = result?.data ?? [];
  const companyOption = useMemo(() => companyTrendOption(rows), [rows]);
  const dailyRows = useMemo(() => [...rows].sort((a, b) => b.snapshot_date.localeCompare(a.snapshot_date)), [rows]);
  const pageCount = Math.max(1, Math.ceil(dailyRows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = dailyRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const coverage = result?.meta.coverage;
  return (
    <>
      <PageHeader eyebrow="变化趋势" title="招聘岗位变化趋势" description="比较各公司的在招岗位、新增与关闭情况。" actions={<><SelectField value={channel} options={channelOptions} onValueChange={(value) => { setChannel(value); setPage(1); }} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <Panel title="公司岗位数量趋势" note={coverage?.snapshot_date ? `更新至 ${coverage.snapshot_date}` : undefined}>
        {loading && !result ? <LoadingBlock /> : rows.length ? <Chart option={companyOption} ariaLabel="各公司岗位规模趋势" className="chart-tall" /> : <EmptyState title="暂无趋势数据" />}
      </Panel>
      <Panel title="每日变化明细" className="section-gap">
        {loading && !result ? <LoadingBlock /> : dailyRows.length ? <><TableWrap><table className="fit-table trend-table"><thead><tr><th>日期</th><th>公司</th><th>招聘类型</th><th>在招岗位</th><th>新增</th><th>内容变化</th><th>关闭</th></tr></thead><tbody>{visibleRows.map((row) => <tr key={`${row.snapshot_date}-${row.source_id}-${row.channel}`}><td>{row.snapshot_date}</td><td><CompanyName companyKey={row.company_key} companyName={row.company_name} /></td><td><ChannelTag channel={row.channel} /></td><td>{formatNumber(row.active_posting_count)}</td><td className="positive">+{formatNumber(row.new_posting_count)}</td><td>{formatNumber(row.changed_posting_count)}</td><td className="negative">-{formatNumber(row.closed_posting_count)}</td></tr>)}</tbody></table></TableWrap><Pagination total={dailyRows.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="条变化记录" /></> : <EmptyState title="暂无每日变化记录" />}
      </Panel>
    </>
  );
}

function companyTrendOption(rows: CompanyRow[]): EChartsOption {
  const dates = [...new Set(rows.map((row) => row.snapshot_date))].sort();
  const companies = [...new Set(rows.map((row) => row.company_name))];
  return {
    color: ["#137a70", "#c5554d", "#2f6f9f", "#b06a22", "#6d5c8c", "#4f7f52"],
    tooltip: { trigger: "axis" },
    legend: { type: "scroll", bottom: 0, data: companies },
    grid: { left: 52, right: 22, top: 24, bottom: 54 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#e9eef0" } } },
    series: companies.map((company) => ({
      name: company,
      type: "line",
      smooth: true,
      connectNulls: false,
      data: dates.map((day) => rows.filter((row) => row.company_name === company && row.snapshot_date === day).reduce((sum, row) => sum + row.active_posting_count, 0) || null),
    })),
  };
}
