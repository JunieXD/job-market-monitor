"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BriefcaseBusiness,
  Building2,
  Clock3,
  MapPinned,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";

import { Chart } from "@/components/Chart";
import {
  type CategoryRow,
  type CityRow,
  type CompanyRow,
  type Coverage,
  type Envelope,
  formatNumber,
  getJson,
  type JobRow,
} from "@/lib/api";

type DashboardData = {
  overview: Envelope<CompanyRow>;
  trend: Envelope<CompanyRow>;
  categories: Envelope<CategoryRow>;
  cities: Envelope<CityRow>;
  jobs: Envelope<JobRow>;
};

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

export function Dashboard() {
  const [channel, setChannel] = useState("all");
  const [data, setData] = useState<DashboardData | null>(null);
  const [coverage, setCoverage] = useState<Coverage>(emptyCoverage);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const params = channel === "all" ? undefined : { channel };
    try {
      const [overview, trend, categories, cities, jobs] = await Promise.all([
        getJson<Envelope<CompanyRow>>("/api/v1/overview", params),
        getJson<Envelope<CompanyRow>>("/api/v1/trends/market", params),
        getJson<Envelope<CategoryRow>>("/api/v1/distributions/categories", params),
        getJson<Envelope<CityRow>>("/api/v1/distributions/cities", params),
        getJson<Envelope<JobRow>>("/api/v1/jobs", { ...params, limit: "12" }),
      ]);
      setData({ overview, trend, categories, cities, jobs });
      setCoverage(overview.meta.coverage);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "暂时无法读取分析数据");
    } finally {
      setLoading(false);
    }
  }, [channel]);

  useEffect(() => { void load(); }, [load]);

  const overviewRows = data?.overview.data ?? [];
  const trendRows = data?.trend.data ?? [];
  const activeTotal = overviewRows.reduce((sum, row) => sum + row.active_posting_count, 0);
  const newTotal = overviewRows.reduce((sum, row) => sum + row.new_posting_count, 0);
  const closedTotal = overviewRows.reduce((sum, row) => sum + row.closed_posting_count, 0);
  const companyCount = new Set(overviewRows.map((row) => row.company_key)).size;
  const asOf = coverage.snapshot_date ?? "暂无标准快照";

  const trendOption = useMemo<EChartsOption>(() => {
    const grouped = new Map<string, { active: number; added: number; closed: number }>();
    trendRows.forEach((row) => {
      const item = grouped.get(row.snapshot_date) ?? { active: 0, added: 0, closed: 0 };
      item.active += row.active_posting_count;
      item.added += row.new_posting_count;
      item.closed += row.closed_posting_count;
      grouped.set(row.snapshot_date, item);
    });
    const dates = [...grouped.keys()].sort();
    return {
      color: ["#0d766e", "#a96416", "#bd5449"],
      tooltip: { trigger: "axis" },
      legend: { bottom: 0, data: ["来源岗位条目合计", "新增", "关闭"] },
      grid: { left: 42, right: 18, top: 18, bottom: 44 },
      xAxis: { type: "category", data: dates, axisLabel: { color: "#687582" } },
      yAxis: { type: "value", axisLabel: { color: "#687582" }, splitLine: { lineStyle: { color: "#edf0f2" } } },
      series: [
        { name: "来源岗位条目合计", type: "line", smooth: true, data: dates.map((date) => grouped.get(date)?.active ?? 0) },
        { name: "新增", type: "bar", barMaxWidth: 18, data: dates.map((date) => grouped.get(date)?.added ?? 0) },
        { name: "关闭", type: "bar", barMaxWidth: 18, data: dates.map((date) => grouped.get(date)?.closed ?? 0) },
      ],
    };
  }, [trendRows]);

  const categoryOption = useMemo<EChartsOption>(() => {
    const rows = [...(data?.categories.data ?? [])]
      .sort((a, b) => b.posting_count - a.posting_count)
      .slice(0, 10);
    return {
      color: ["#0d766e"],
      grid: { left: 96, right: 20, top: 10, bottom: 24 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", axisLabel: { color: "#687582" }, splitLine: { lineStyle: { color: "#edf0f2" } } },
      yAxis: { type: "category", data: rows.map((row) => row.canonical_category_name ?? row.source_category_name ?? "未分类"), axisLabel: { color: "#687582" } },
      series: [{ type: "bar", barMaxWidth: 22, data: rows.map((row) => row.posting_count) }],
    };
  }, [data?.categories.data]);

  const cityOption = useMemo<EChartsOption>(() => {
    const rows = [...(data?.cities.data ?? [])].sort((a, b) => b.posting_count - a.posting_count).slice(0, 10);
    return {
      color: ["#bd5449"],
      grid: { left: 72, right: 20, top: 10, bottom: 24 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", axisLabel: { color: "#687582" }, splitLine: { lineStyle: { color: "#edf0f2" } } },
      yAxis: { type: "category", data: rows.map((row) => row.city_name), axisLabel: { color: "#687582" } },
      series: [{ type: "bar", barMaxWidth: 22, data: rows.map((row) => row.posting_count) }],
    };
  }, [data?.cities.data]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><Activity size={20} /></div>
          <div><h1>就业市场监测器</h1><p>官方招聘岗位数据工作台</p></div>
        </div>
        <div className="topbar-meta">
          <span className={`status-dot ${coverage.coverage_ratio < 1 ? "warning" : ""}`} />
          <span>{coverage.coverage_ratio < 1 ? "部分来源覆盖" : "标准快照覆盖"}</span>
          <span>数据日期：{asOf}</span>
        </div>
      </header>

      <main className="content">
        <div className="toolbar">
          <div><h2>市场总览</h2><p>岗位条目趋势、城市分布和来源质量</p></div>
          <div className="filters">
            <label className="filter">渠道
              <select value={channel} onChange={(event) => setChannel(event.target.value)}>
                <option value="all">全部渠道</option>
                <option value="experienced">社会招聘</option>
                <option value="campus">校园招聘</option>
                <option value="internship">实习招聘</option>
              </select>
            </label>
            <button className="refresh" type="button" onClick={() => void load()} title="刷新数据">
              <RefreshCw size={15} /> 刷新
            </button>
          </div>
        </div>

        {error && <div className="notice error"><AlertTriangle size={17} /><span>{error}。请确认 API 服务已启动。</span></div>}
        {!loading && !error && coverage.coverage_ratio < 1 && (
          <div className="notice"><AlertTriangle size={17} /><span>当前日期只覆盖了 {coverage.standard_snapshot_count} / {coverage.configured_source_channel_count} 个来源渠道，趋势仅代表已完成标准快照的来源。</span></div>
        )}

        <section className="kpi-grid" aria-label="核心指标">
          <Kpi icon={<BriefcaseBusiness size={16} />} label="来源岗位条目合计" value={formatNumber(activeTotal)} tone="teal" sub="不同来源未做跨站去重" />
          <Kpi icon={<TrendingUp size={16} />} label="当日新增条目" value={formatNumber(newTotal)} tone="amber" sub={`覆盖 ${formatNumber(companyCount)} 家公司`} />
          <Kpi icon={<Clock3 size={16} />} label="当日关闭条目" value={formatNumber(closedTotal)} tone="coral" sub="仅来自权威标准快照" />
          <Kpi icon={<ShieldCheck size={16} />} label="标准快照覆盖率" value={`${Math.round(coverage.coverage_ratio * 100)}%`} tone="teal" sub={`${coverage.absence_authoritative_source_channel_count} 个渠道具备缺失判断权威性`} />
        </section>

        <section className="grid">
          <Panel className="span-8" icon={<BarChart3 size={17} />} title="岗位变化趋势" note="来源岗位条目合计，不等同于招聘人数">
            {loading ? <Loading /> : trendRows.length ? <Chart option={trendOption} ariaLabel="岗位新增、关闭和活跃条目趋势图" /> : <Empty title="暂无趋势数据" />}
          </Panel>
          <Panel className="span-4" icon={<Building2 size={17} />} title="来源覆盖" note="按公司和招聘来源分别统计">
            {loading ? <Loading /> : overviewRows.length ? <SourceTable rows={overviewRows} /> : <Empty title="暂无来源数据" />}
          </Panel>
          <Panel className="span-6" icon={<BarChart3 size={17} />} title="岗位分类排行" note="未分类与未映射单列，不自动生成类别">
            {loading ? <Loading /> : data?.categories.data.length ? <Chart option={categoryOption} ariaLabel="岗位分类排行图" /> : <Empty title="暂无分类数据" />}
          </Panel>
          <Panel className="span-6" icon={<MapPinned size={17} />} title="城市岗位排行" note="岗位与城市有关联即计数，多城市岗位可能重复">
            {loading ? <Loading /> : data?.cities.data.length ? <Chart option={cityOption} ariaLabel="城市岗位排行图" /> : <Empty title="暂无城市数据" />}
          </Panel>
          <Panel className="span-12" icon={<BriefcaseBusiness size={17} />} title="最新岗位" note="当前岗位列表，点击标题可打开官网详情">
            {loading ? <Loading /> : data?.jobs.data.length ? <JobTable rows={data.jobs.data} /> : <Empty title="暂无岗位数据" />}
          </Panel>
        </section>
        <p className="footer">数据来自公开招聘官网。岗位条目数不等于招聘人数，页面以每日标准快照和来源覆盖状态为准。</p>
      </main>
    </div>
  );
}

function Kpi({ icon, label, value, sub, tone }: { icon: React.ReactNode; label: string; value: string; sub: string; tone: string }) {
  return <article className={`kpi ${tone}`}><div className="kpi-label"><span>{label}</span>{icon}</div><div className="kpi-value">{value}</div><div className="kpi-sub">{sub}</div></article>;
}

function Panel({ children, className, icon, title, note }: { children: React.ReactNode; className: string; icon: React.ReactNode; title: string; note: string }) {
  return <article className={`panel ${className}`}><div className="panel-head"><div><h3 className="panel-title">{icon} {title}</h3><p className="panel-note">{note}</p></div></div>{children}</article>;
}

function SourceTable({ rows }: { rows: CompanyRow[] }) {
  return <div className="table-wrap"><table><thead><tr><th>公司</th><th>渠道</th><th>岗位条目</th></tr></thead><tbody>{rows.slice(0, 8).map((row) => <tr key={`${row.company_key}-${row.source_id}-${row.channel}`}><td>{row.company_name}</td><td><span className="source-pill">{row.channel}</span></td><td>{formatNumber(row.active_posting_count)}</td></tr>)}</tbody></table></div>;
}

function JobTable({ rows }: { rows: JobRow[] }) {
  return <div className="table-wrap"><table><thead><tr><th>职位</th><th>公司</th><th>渠道</th><th>发布时间</th><th>招聘人数</th><th>来源</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.source_key}-${row.external_id}`}><td className="title">{row.title}<span>{row.external_id}</span></td><td>{row.company_name}</td><td><span className="source-pill">{row.channel}</span></td><td>{row.published_at ? new Date(row.published_at).toLocaleDateString("zh-CN") : "未提供"}</td><td>{row.recruitment_count ?? "未提供"}</td><td><a href={row.source_url} target="_blank" rel="noreferrer">官网</a></td></tr>)}</tbody></table></div>;
}

function Loading() { return <div className="empty"><div><div className="skeleton" style={{ width: 180 }} /><div className="skeleton" style={{ width: 120, marginTop: 10 }} /></div></div>; }
function Empty({ title }: { title: string }) { return <div className="empty"><div><strong>{title}</strong><span>当前筛选条件下没有可展示的标准快照。</span></div></div>; }
