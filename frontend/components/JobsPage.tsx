"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, ExternalLink, Search } from "lucide-react";

import { SelectField } from "@/components/SelectField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyMeta, type Envelope, formatNumber, getJson, type JobRow } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

const pageSize = 25;

export function JobsPage() {
  const [channel, setChannel] = useState("all");
  const [company, setCompany] = useState("all");
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<Envelope<JobRow> | null>(null);
  const [companies, setCompanies] = useState<CompanyMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [jobs, companyResult] = await Promise.all([
        getJson<Envelope<JobRow>>("/api/v1/jobs", {
          channel: channel === "all" ? undefined : channel,
          company_key: company === "all" ? undefined : company,
          query: query || undefined,
          limit: pageSize,
          offset,
        }),
        getJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies"),
      ]);
      setResult(jobs); setCompanies(companyResult.data);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取岗位数据"); }
    finally { setLoading(false); }
  }, [channel, company, query, offset]);
  useEffect(() => { void load(); }, [load]);

  const companyOptions = useMemo(() => [{ value: "all", label: "全部公司" }, ...companies.map((item) => ({ value: item.key, label: item.name }))], [companies]);
  const total = result?.meta.pagination?.total ?? 0;
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const coverage = result?.meta.coverage;
  function submit(event: FormEvent) { event.preventDefault(); setOffset(0); setQuery(draft.trim()); }
  function changeChannel(value: string) { setOffset(0); setChannel(value); }
  function changeCompany(value: string) { setOffset(0); setCompany(value); }

  return (
    <>
      <PageHeader eyebrow="岗位查询" title="查看当前仍在招聘的岗位" description="所有岗位都能回到企业招聘官网核对；列表默认使用最新一个标准日快照。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <form className="search-toolbar" onSubmit={submit}>
        <div className="search-input"><Search size={16} /><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="搜索岗位名称或职位描述" aria-label="搜索岗位" /></div>
        <SelectField value={company} options={companyOptions} onValueChange={changeCompany} ariaLabel="选择公司" />
        <SelectField value={channel} options={channelOptions} onValueChange={changeChannel} ariaLabel="选择招聘类型" />
        <button type="submit" className="primary-button"><Search size={15} /><span>搜索</span></button>
      </form>
      <Panel title="岗位列表" note={`共找到 ${formatNumber(total)} 个岗位条目，数据日期：${coverage?.snapshot_date ?? "暂无"}`}>
        {loading ? <LoadingBlock /> : result?.data.length ? <JobTable rows={result.data} /> : <EmptyState title="没有找到符合条件的岗位" detail="可以调整公司、招聘类型或搜索词后重试。" />}
        <div className="pagination">
          <button type="button" className="icon-button" aria-label="上一页" title="上一页" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - pageSize))}><ArrowLeft size={17} /></button>
          <span>第 {page} / {pages} 页</span>
          <button type="button" className="icon-button" aria-label="下一页" title="下一页" disabled={offset + pageSize >= total || loading} onClick={() => setOffset(offset + pageSize)}><ArrowRight size={17} /></button>
        </div>
      </Panel>
    </>
  );
}

function JobTable({ rows }: { rows: JobRow[] }) {
  return <TableWrap><table><thead><tr><th>岗位</th><th>公司</th><th>招聘类型</th><th>官网更新时间</th><th>最近采集</th><th><span className="sr-only">官网链接</span></th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.source_key}-${row.external_id}`}><td className="job-title"><strong>{row.title}</strong><span>{row.external_id}</span></td><td>{row.company_name}</td><td><ChannelTag channel={row.channel} /></td><td>{row.source_updated_at ? new Date(row.source_updated_at).toLocaleDateString("zh-CN") : "官网未提供"}</td><td>{new Date(row.last_seen_at).toLocaleDateString("zh-CN")}</td><td><a className="external-link" href={row.source_url} target="_blank" rel="noreferrer" aria-label={`在官网查看 ${row.title}`}><ExternalLink size={15} /><span>官网</span></a></td></tr>)}</tbody></table></TableWrap>;
}
