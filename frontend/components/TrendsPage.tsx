"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { SelectField } from "@/components/SelectField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyRow, type Envelope, formatNumber, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

export function TrendsPage() {
  const [channel, setChannel] = useState("all");
  const [result, setResult] = useState<Envelope<CompanyRow> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      setResult(await getJson<Envelope<CompanyRow>>("/api/v1/trends/companies", channel === "all" ? undefined : { channel }));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取趋势数据"); }
    finally { setLoading(false); }
  }, [channel]);
  useEffect(() => { void load(); }, [load]);

  const rows = result?.data ?? [];
  const companyOption = useMemo(() => companyTrendOption(rows), [rows]);
  const dailyRows = useMemo(() => [...rows].sort((a, b) => b.snapshot_date.localeCompare(a.snapshot_date)).slice(0, 30), [rows]);
  const coverage = result?.meta.coverage;
  return (
    <>
      <PageHeader eyebrow="变化趋势" title="市场是在扩张还是收缩" description="按公司比较每日在招岗位规模，并观察新增、关闭和岗位内容变化。" actions={<><SelectField value={channel} options={channelOptions} onValueChange={setChannel} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <Panel title="公司岗位规模趋势" note="每条线代表一家已完成标准快照的公司">
        {loading ? <LoadingBlock /> : rows.length ? <Chart option={companyOption} ariaLabel="各公司岗位规模趋势" className="chart-tall" /> : <EmptyState title="至少需要一个日快照" />}
      </Panel>
      <Panel title="每日变化明细" note="基线日用于建立初始存量，因此不计新增" className="section-gap">
        {loading ? <LoadingBlock /> : dailyRows.length ? <TableWrap><table><thead><tr><th>日期</th><th>公司</th><th>招聘类型</th><th className="numeric">在招岗位</th><th className="numeric">新增</th><th className="numeric">内容变化</th><th className="numeric">关闭</th></tr></thead><tbody>{dailyRows.map((row) => <tr key={`${row.snapshot_date}-${row.source_id}-${row.channel}`}><td>{row.snapshot_date}</td><td>{row.company_name}</td><td><ChannelTag channel={row.channel} /></td><td className="numeric">{formatNumber(row.active_posting_count)}</td><td className="numeric positive">+{formatNumber(row.new_posting_count)}</td><td className="numeric">{formatNumber(row.changed_posting_count)}</td><td className="numeric negative">-{formatNumber(row.closed_posting_count)}</td></tr>)}</tbody></table></TableWrap> : <EmptyState title="还没有每日变化记录" />}
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
