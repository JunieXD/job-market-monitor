"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Clock3, DatabaseZap, LoaderCircle, TriangleAlert } from "lucide-react";

import { ChannelTag, EmptyState, ErrorNotice, LoadingBlock, MetricCard, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CollectionStatus, formatDateTime, formatNumber, formatPercent, getJson } from "@/lib/api";
import { collectionStateLabel } from "@/lib/labels";

export function CollectionPage() {
  const [data, setData] = useState<CollectionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try { setData(await getJson<CollectionStatus>("/api/v1/collection/status")); setError(null); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取采集状态"); }
    finally { if (!quiet) setLoading(false); }
  }, []);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(true); }, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const summary = data?.summary;
  const scheduleText = data ? `每天 ${String(data.schedule.hour).padStart(2, "0")}:${String(data.schedule.minute).padStart(2, "0")} 自动采集` : "每天自动采集";
  const sorted = useMemo(() => data?.channels ?? [], [data?.channels]);
  return (
    <>
      <PageHeader eyebrow="采集运行" title="今天的数据采集到哪一步了" description="页面每 15 秒自动更新。单个招聘网站失败不会阻止其他来源继续采集。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      <div className="schedule-strip"><div><Clock3 size={18} /><span>{scheduleText}</span></div><div><span>下次计划运行</span><strong>{formatDateTime(data?.schedule.next_run_at)}</strong></div><div><span>当前数据日期</span><strong>{data?.snapshot_date ?? "暂无"}</strong></div></div>
      <section className="metric-grid" aria-label="采集进度指标">
        <MetricCard icon={<DatabaseZap size={17} />} label="今日完成进度" value={formatPercent(summary?.progress_ratio)} detail={`${summary?.completed ?? 0} / ${summary?.total ?? 0} 个来源渠道`} />
        <MetricCard icon={<LoaderCircle size={17} />} label="正在采集" value={formatNumber(summary?.running)} detail={`最近活动 ${formatDateTime(summary?.last_activity_at)}`} tone="blue" />
        <MetricCard icon={<CheckCircle2 size={17} />} label="采集完成" value={formatNumber(summary?.completed)} detail="已生成今日标准快照" tone="amber" />
        <MetricCard icon={<TriangleAlert size={17} />} label="需要关注" value={formatNumber((summary?.failed ?? 0) + (summary?.partial ?? 0))} detail={`${summary?.failed ?? 0} 个失败，${summary?.partial ?? 0} 个结果不完整`} tone="coral" />
      </section>
      <Panel title="今日采集进度" note="完成状态表示该来源渠道已经生成当天最新标准快照">
        <div className="progress-track" aria-label={`今日采集完成 ${formatPercent(summary?.progress_ratio)}`}><span style={{ width: `${Math.round((summary?.progress_ratio ?? 0) * 100)}%` }} /></div>
        {loading ? <LoadingBlock /> : sorted.length ? <TableWrap><table><thead><tr><th>公司与招聘站</th><th>招聘类型</th><th>状态</th><th>开始时间</th><th className="numeric">岗位</th><th className="numeric">页面</th><th>最近标准快照</th></tr></thead><tbody>{sorted.map((row) => <tr key={`${row.source_key}-${row.channel}`}><td><strong>{row.company_name}</strong><span className="cell-note">{row.display_name}</span>{row.error_summary && <span className="cell-error">{row.error_summary}</span>}</td><td><ChannelTag channel={row.channel} /></td><td><span className={`status-badge ${row.state}`}>{row.state === "running" && <LoaderCircle size={13} className="spin" />}{collectionStateLabel(row.state)}</span></td><td>{formatDateTime(row.started_at)}</td><td className="numeric">{row.discovered_count == null ? "-" : formatNumber(row.discovered_count)}</td><td className="numeric">{row.page_count == null ? "-" : formatNumber(row.page_count)}</td><td>{row.last_standard_date ?? "尚未完成"}</td></tr>)}</tbody></table></TableWrap> : <EmptyState title="还没有配置采集来源" />}
      </Panel>
    </>
  );
}
