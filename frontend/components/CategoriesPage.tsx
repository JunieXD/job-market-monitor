"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { SelectField } from "@/components/SelectField";
import { CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CategoryRow, type Envelope, formatNumber, formatPercent, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

type GroupedCategory = { key: string; name: string; status: string; count: number; share: number };

export function CategoriesPage() {
  const [channel, setChannel] = useState("all");
  const [result, setResult] = useState<Envelope<CategoryRow> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setResult(await getJson<Envelope<CategoryRow>>("/api/v1/distributions/categories", channel === "all" ? undefined : { channel })); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取分类数据"); }
    finally { setLoading(false); }
  }, [channel]);
  useEffect(() => { void load(); }, [load]);
  const rows = useMemo(() => groupCategories(result?.data ?? []), [result?.data]);
  const chart = useMemo(() => categoryOption(rows), [rows]);
  const coverage = result?.meta.coverage;
  const mapped = rows.filter((row) => row.status === "mapped").reduce((sum, row) => sum + row.count, 0);
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  return (
    <>
      <PageHeader eyebrow="岗位分类" title="企业主要在招聘哪些岗位" description="优先使用招聘官网提供的分类；没有可靠映射的岗位会明确保留为未分类或未映射。" actions={<><SelectField value={channel} options={channelOptions} onValueChange={setChannel} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <div className="insight-line"><span>已映射岗位占比</span><strong>{formatPercent(total ? mapped / total : 0)}</strong><p>系统不会在没有正式分类规则或模型评估时自动生成岗位标签。</p></div>
      <div className="content-grid">
        <Panel className="span-7" title="岗位分类分布" note={`数据日期：${coverage?.snapshot_date ?? "暂无"}`}>
          {loading ? <LoadingBlock /> : rows.length ? <Chart option={chart} ariaLabel="岗位分类分布" className="chart-tall" /> : <EmptyState title="暂无岗位分类数据" />}
        </Panel>
        <Panel className="span-5" title="分类排行" note="不同招聘类型已合并统计">
          {loading ? <LoadingBlock /> : rows.length ? <TableWrap><table className="compact-table"><thead><tr><th>分类</th><th>数据状态</th><th className="numeric">岗位</th><th className="numeric">占比</th></tr></thead><tbody>{rows.map((row) => <tr key={row.key}><td>{row.name}</td><td><span className={`tag ${row.status}`}>{categoryStatus(row.status)}</span></td><td className="numeric">{formatNumber(row.count)}</td><td className="numeric">{formatPercent(row.share)}</td></tr>)}</tbody></table></TableWrap> : <EmptyState title="暂无分类排行" />}
        </Panel>
      </div>
    </>
  );
}

function groupCategories(rows: CategoryRow[]): GroupedCategory[] {
  const grouped = new Map<string, GroupedCategory>();
  rows.forEach((row) => {
    const key = row.canonical_category_key ?? row.source_category_name ?? "unclassified";
    const current = grouped.get(key) ?? { key, name: row.canonical_category_name ?? row.source_category_name ?? "未分类", status: row.category_status ?? "unclassified", count: 0, share: 0 };
    current.count += row.posting_count;
    grouped.set(key, current);
  });
  const total = [...grouped.values()].reduce((sum, row) => sum + row.count, 0);
  return [...grouped.values()].map((row) => ({ ...row, share: total ? row.count / total : 0 })).sort((a, b) => b.count - a.count);
}

function categoryOption(rows: GroupedCategory[]): EChartsOption {
  const top = rows.slice(0, 12).reverse();
  return { color: ["#137a70"], tooltip: { trigger: "axis", axisPointer: { type: "shadow" } }, grid: { left: 110, right: 24, top: 12, bottom: 26 }, xAxis: { type: "value", splitLine: { lineStyle: { color: "#e9eef0" } } }, yAxis: { type: "category", data: top.map((row) => row.name) }, series: [{ type: "bar", barMaxWidth: 22, data: top.map((row) => row.count) }] };
}

function categoryStatus(status: string): string {
  if (status === "mapped") return "已统一";
  if (status === "unmapped") return "待映射";
  return "官网未分类";
}
