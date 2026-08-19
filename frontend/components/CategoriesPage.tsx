"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

import { Chart } from "@/components/Chart";
import { Pagination } from "@/components/Pagination";
import { SelectField } from "@/components/SelectField";
import { CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CategoryRow, type Envelope, formatNumber, formatPercent, getCachedJson, getJson } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

type GroupedCategory = { key: string; name: string; status: string; count: number; share: number };

export function CategoriesPage() {
  const [channel, setChannel] = useState("all");
  const [result, setResult] = useState<Envelope<CategoryRow> | null>(() => getCachedJson("/api/v1/distributions/categories"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const load = useCallback(async () => {
    const params = channel === "all" ? undefined : { channel };
    const cached = getCachedJson<Envelope<CategoryRow>>("/api/v1/distributions/categories", params);
    if (cached) setResult(cached);
    setLoading(!cached); setError(null);
    try { setResult(await getJson<Envelope<CategoryRow>>("/api/v1/distributions/categories", params)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取分类数据"); }
    finally { setLoading(false); }
  }, [channel]);
  useEffect(() => { void load(); }, [load]);
  const rows = useMemo(() => groupCategories(result?.data ?? []), [result?.data]);
  const chart = useMemo(() => categoryOption(rows), [rows]);
  const coverage = result?.meta.coverage;
  const mapped = rows.filter((row) => row.status === "mapped").reduce((sum, row) => sum + row.count, 0);
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  return (
    <>
      <PageHeader eyebrow="岗位分类" title="岗位类别分布" description="了解不同类别岗位的数量与市场占比。" actions={<><SelectField value={channel} options={channelOptions} onValueChange={(value) => { setChannel(value); setPage(1); }} ariaLabel="选择招聘类型" /><RefreshButton onClick={() => void load()} loading={loading} /></>} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <div className="insight-line"><span>已归类岗位占比</span><strong>{formatPercent(total ? mapped / total : 0)}</strong><p>便于比较不同招聘网站的岗位类别</p></div>
      <div className="content-grid">
        <Panel className="span-7" title="岗位分类分布" note={coverage?.snapshot_date ? `更新至 ${coverage.snapshot_date}` : "暂无数据"}>
          {loading && !result ? <LoadingBlock /> : rows.length ? <Chart option={chart} ariaLabel="岗位分类分布" className="chart-tall" /> : <EmptyState title="暂无岗位分类数据" />}
        </Panel>
        <Panel className="span-5" title="分类排行" note={coverage?.snapshot_date ? `更新至 ${coverage.snapshot_date}` : "暂无数据"}>
          {loading && !result ? <LoadingBlock /> : rows.length ? <><TableWrap><table className="compact-table fit-table category-table"><thead><tr><th>分类</th><th>分类状态</th><th>岗位</th><th>占比</th></tr></thead><tbody>{visibleRows.map((row) => <tr key={row.key}><td>{row.name}</td><td><span className={`tag ${row.status}`}>{categoryStatus(row.status)}</span></td><td>{formatNumber(row.count)}</td><td>{formatPercent(row.share)}</td></tr>)}</tbody></table></TableWrap><Pagination total={rows.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个分类" /></> : <EmptyState title="暂无分类排行" />}
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
  if (status === "mapped") return "已归类";
  if (status === "unmapped") return "待归类";
  return "官网未分类";
}
