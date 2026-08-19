"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLink, Search } from "lucide-react";

import { MultiSelectFilter } from "@/components/MultiSelectFilter";
import { Pagination } from "@/components/Pagination";
import { SearchField } from "@/components/SearchField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyMeta, type Envelope, formatNumber, getJson, type JobRow } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

const searchScopes = [
  { value: "all", label: "全部字段" },
  { value: "title", label: "岗位名称" },
  { value: "description", label: "岗位描述" },
  { value: "requirements", label: "岗位要求" },
];
const filterChannelOptions = channelOptions.filter((option) => option.value !== "all");

export function JobsPage() {
  const [channels, setChannels] = useState<string[] | null>(null);
  const [companyKeys, setCompanyKeys] = useState<string[] | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [queryField, setQueryField] = useState("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [result, setResult] = useState<Envelope<JobRow> | null>(null);
  const [companies, setCompanies] = useState<CompanyMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [jobs, companyResult] = await Promise.all([
        getJson<Envelope<JobRow>>("/api/v1/jobs", {
          channels: channels === null ? undefined : channels.length ? channels : ["__none__"],
          company_keys: companyKeys === null ? undefined : companyKeys.length ? companyKeys : ["__none__"],
          query: query || undefined,
          query_field: queryField,
          limit: pageSize,
          offset: (page - 1) * pageSize,
        }),
        getJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies"),
      ]);
      setResult(jobs); setCompanies(companyResult.data);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取岗位数据"); }
    finally { setLoading(false); }
  }, [channels, companyKeys, query, queryField, page, pageSize]);
  useEffect(() => { void load(); }, [load]);

  const companyOptions = useMemo(() => companies.map((item) => ({ value: item.key, label: item.name })), [companies]);
  const total = result?.meta.pagination?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(page, pageCount);
  const coverage = result?.meta.coverage;
  function submit(event: FormEvent) { event.preventDefault(); setPage(1); setQuery(draft.trim()); }

  return (
    <>
      <PageHeader eyebrow="岗位查询" title="查看当前仍在招聘的岗位" description="所有岗位都能回到企业招聘官网核对；列表默认使用最新一个标准日快照。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <form className="search-toolbar jobs-toolbar" onSubmit={submit}>
        <SearchField value={draft} onValueChange={setDraft} scope={queryField} scopes={searchScopes} onScopeChange={(value) => { setQueryField(value); setPage(1); }} placeholder="支持间隔关键词，例如“字跳”" ariaLabel="搜索岗位" />
        <MultiSelectFilter label="公司" options={companyOptions} values={companyKeys} onValuesChange={(values) => { setCompanyKeys(values); setPage(1); }} ariaLabel="筛选公司" />
        <MultiSelectFilter label="招聘类型" options={filterChannelOptions} values={channels} onValuesChange={(values) => { setChannels(values); setPage(1); }} ariaLabel="筛选招聘类型" />
        <button type="submit" className="primary-button"><Search size={15} /><span>搜索</span></button>
      </form>
      <Panel title="岗位列表" note={`共找到 ${formatNumber(total)} 个岗位条目，数据日期：${coverage?.snapshot_date ?? "暂无"}`}>
        {loading ? <LoadingBlock /> : result?.data.length ? <JobTable rows={result.data} /> : <EmptyState title="没有找到符合条件的岗位" detail="可以调整公司、招聘类型或搜索词后重试。" />}
        <Pagination total={total} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个岗位" />
      </Panel>
    </>
  );
}

function JobTable({ rows }: { rows: JobRow[] }) {
  return <TableWrap><table><thead><tr><th>岗位</th><th>公司</th><th>招聘类型</th><th>官网更新时间</th><th>最近采集</th><th><span className="sr-only">官网链接</span></th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.source_key}-${row.external_id}`}><td className="job-title"><strong>{row.title}</strong><span>{row.external_id}</span></td><td>{row.company_name}</td><td><ChannelTag channel={row.channel} /></td><td>{row.source_updated_at ? new Date(row.source_updated_at).toLocaleDateString("zh-CN") : "官网未提供"}</td><td>{new Date(row.last_seen_at).toLocaleDateString("zh-CN")}</td><td><a className="external-link" href={row.source_url} target="_blank" rel="noreferrer" aria-label={`在官网查看 ${row.title}`}><ExternalLink size={15} /><span>官网</span></a></td></tr>)}</tbody></table></TableWrap>;
}
