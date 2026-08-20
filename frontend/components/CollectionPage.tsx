"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Clock3, DatabaseZap, LoaderCircle, TriangleAlert } from "lucide-react";

import { CompanyName } from "@/components/CompanyLogo";
import { MultiSelectFilter } from "@/components/MultiSelectFilter";
import { Pagination } from "@/components/Pagination";
import { SearchField } from "@/components/SearchField";
import { ChannelTag, EmptyState, ErrorNotice, LoadingBlock, MetricCard, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CollectionStatus, formatDateTime, formatNumber, formatPercent, getCachedJson, getJson, getStoredJson } from "@/lib/api";
import { collectionStateLabel } from "@/lib/labels";
import { channelOptions } from "@/lib/labels";
import { matchesSubsequence } from "@/lib/search";

const searchScopes = [
  { value: "company", label: "公司名称" },
  { value: "source", label: "招聘站名称" },
  { value: "error", label: "异常信息" },
];
const stateOptions = ["running", "failed", "partial", "completed", "pending"].map((value) => ({ value, label: collectionStateLabel(value) }));
const filterChannelOptions = channelOptions.filter((option) => option.value !== "all");

export function CollectionPage() {
  const [data, setData] = useState<CollectionStatus | null>(() => getCachedJson("/api/v1/collection/status"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [queryFields, setQueryFields] = useState<string[] | null>(null);
  const [companies, setCompanies] = useState<string[] | null>(null);
  const [channels, setChannels] = useState<string[] | null>(null);
  const [states, setStates] = useState<string[] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const load = useCallback(async (quiet = false): Promise<CollectionStatus | null> => {
    const cached = getStoredJson<CollectionStatus>("/api/v1/collection/status");
    if (!quiet) setLoading(!cached);
    if (cached) setData(cached);
    try {
      const next = await getJson<CollectionStatus>("/api/v1/collection/status");
      setData(next); setError(null); return next;
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取采集状态"); }
    finally { if (!quiet) setLoading(false); }
    return null;
  }, []);
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    async function refresh() {
      const next = await load(true);
      if (!stopped) timer = window.setTimeout(refresh, next?.summary.running ? 5_000 : 30_000);
    }
    void load().then((next) => {
      if (!stopped) timer = window.setTimeout(refresh, next?.summary.running ? 5_000 : 30_000);
    });
    return () => { stopped = true; if (timer) window.clearTimeout(timer); };
  }, [load]);

  const summary = data?.summary;
  const scheduleText = data ? `每天 ${String(data.schedule.hour).padStart(2, "0")}:${String(data.schedule.minute).padStart(2, "0")} 自动采集` : "每天自动采集";
  const sorted = useMemo(() => data?.channels ?? [], [data?.channels]);
  const companyOptions = useMemo(() => [...new Map(sorted.map((row) => [row.company_key, { value: row.company_key, label: row.company_name, companyKey: row.company_key }])).values()], [sorted]);
  const filtered = useMemo(() => sorted.filter((row) => {
    if (companies !== null && !companies.includes(row.company_key)) return false;
    if (channels !== null && !channels.includes(row.channel)) return false;
    if (states !== null && !states.includes(row.state)) return false;
    const selectedFields = queryFields ?? searchScopes.map((scope) => scope.value);
    const fields = selectedFields.map((field) => field === "company" ? row.company_name : field === "source" ? row.display_name : row.error_summary ?? "");
    return fields.some((field) => matchesSubsequence(field, query));
  }), [channels, companies, query, queryFields, sorted, states]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  return (
    <>
      <PageHeader eyebrow="数据采集" title="今日数据采集情况" description="查看各招聘网站的数据更新时间与采集进度。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      <div className="schedule-strip"><div><Clock3 size={18} /><span>{scheduleText}</span></div><div><span>下次采集</span><strong>{formatDateTime(data?.schedule.next_run_at)}</strong></div><div><span>数据更新至</span><strong>{data?.snapshot_date ?? "暂无"}</strong></div></div>
      <section className="metric-grid" aria-label="采集进度指标">
        <MetricCard icon={<DatabaseZap size={17} />} label="采集进度" value={formatPercent(summary?.progress_ratio)} detail={`${summary?.completed ?? 0} / ${summary?.total ?? 0} 个招聘渠道`} />
        <MetricCard icon={<LoaderCircle size={17} />} label="采集中" value={formatNumber(summary?.running)} detail="运行中的招聘渠道" tone="blue" />
        <MetricCard icon={<CheckCircle2 size={17} />} label="已完成" value={formatNumber(summary?.completed)} detail="今日已更新的招聘渠道" tone="amber" />
        <MetricCard icon={<TriangleAlert size={17} />} label="异常" value={formatNumber((summary?.failed ?? 0) + (summary?.partial ?? 0))} detail={`${summary?.failed ?? 0} 个失败 · ${summary?.partial ?? 0} 个不完整`} tone="coral" />
      </section>
      <div className="search-toolbar collection-toolbar">
        <SearchField value={query} onValueChange={(value) => { setQuery(value); setPage(1); }} scopesValue={queryFields} scopes={searchScopes} onScopesChange={(values) => { setQueryFields(values); setPage(1); }} placeholder="搜索采集记录" ariaLabel="搜索采集来源" />
        <MultiSelectFilter label="公司" options={companyOptions} values={companies} onValuesChange={(values) => { setCompanies(values); setPage(1); }} ariaLabel="筛选采集公司" />
        <MultiSelectFilter label="招聘类型" options={filterChannelOptions} values={channels} onValuesChange={(values) => { setChannels(values); setPage(1); }} ariaLabel="筛选采集招聘类型" />
        <MultiSelectFilter label="状态" options={stateOptions} values={states} onValuesChange={(values) => { setStates(values); setPage(1); }} ariaLabel="筛选采集状态" />
      </div>
      <Panel title="招聘网站采集进度">
        <div className="progress-track" aria-label={`今日采集完成 ${formatPercent(summary?.progress_ratio)}`}><span style={{ width: `${Math.round((summary?.progress_ratio ?? 0) * 100)}%` }} /></div>
        {loading && !data ? <LoadingBlock /> : visibleRows.length ? <><TableWrap><table className="fit-table collection-table"><thead><tr><th>公司与招聘网站</th><th>招聘类型</th><th>状态</th><th>采集开始</th><th>已发现岗位</th><th>已抓页面</th><th>数据更新至</th></tr></thead><tbody>{visibleRows.map((row) => <tr key={`${row.source_key}-${row.channel}`}><td><CompanyName companyKey={row.company_key} companyName={row.company_name} /><span className="cell-note">{row.display_name}</span></td><td><ChannelTag channel={row.channel} /></td><td><span className={`status-badge ${row.state}`}>{row.state === "running" && <LoaderCircle size={13} className="spin" />}{collectionStateLabel(row.state)}</span></td><td>{formatDateTime(row.started_at)}</td><td>{progressValue(row.state, row.discovered_count, "统计中")}</td><td>{progressValue(row.state, row.page_count, "准备中")}</td><td>{row.last_standard_date ?? "-"}</td></tr>)}</tbody></table></TableWrap><Pagination total={filtered.length} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个招聘渠道" /></> : <EmptyState title={sorted.length ? "没有符合条件的招聘渠道" : "暂无采集来源"} detail={sorted.length ? "请调整筛选条件。" : undefined} />}
      </Panel>
    </>
  );
}

function progressValue(state: string, value: number | null, pendingText: string): string {
  if (state === "running" && !value) return pendingText;
  return value == null ? "-" : formatNumber(value);
}
