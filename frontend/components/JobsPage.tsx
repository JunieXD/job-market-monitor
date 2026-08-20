"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLink, Search } from "lucide-react";

import { CompanyName } from "@/components/CompanyLogo";
import { JobDetailDialog } from "@/components/JobDetailDialog";
import { MultiSelectFilter } from "@/components/MultiSelectFilter";
import { Pagination } from "@/components/Pagination";
import { SearchField } from "@/components/SearchField";
import { ChannelTag, CoverageNotice, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Panel, RefreshButton, TableWrap } from "@/components/ui";
import { type CompanyMeta, type Envelope, formatNumber, getCachedJson, getJson, type JobRow } from "@/lib/api";
import { channelOptions } from "@/lib/labels";

const searchScopes = [
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
  const [queryFields, setQueryFields] = useState<string[] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [result, setResult] = useState<Envelope<JobRow> | null>(() => getCachedJson("/api/v1/jobs", { limit: 20, offset: 0 }));
  const [companies, setCompanies] = useState<CompanyMeta[]>(() => getCachedJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies")?.data ?? []);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<JobRow | null>(null);

  const load = useCallback(async () => {
    const params = {
      channels: channels === null ? undefined : channels.length ? channels : ["__none__"],
      company_keys: companyKeys === null ? undefined : companyKeys.length ? companyKeys : ["__none__"],
      query: query || undefined,
      query_fields: query && queryFields?.length ? queryFields : undefined,
      query_fields_empty: query && queryFields !== null && !queryFields.length ? "true" : undefined,
      limit: pageSize,
      offset: (page - 1) * pageSize,
    };
    const cachedJobs = getCachedJson<Envelope<JobRow>>("/api/v1/jobs", params);
    const cachedCompanies = getCachedJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies");
    if (cachedJobs) setResult(cachedJobs);
    if (cachedCompanies) setCompanies(cachedCompanies.data);
    setLoading(!cachedJobs); setError(null);
    try {
      const [jobs, companyResult] = await Promise.all([
        getJson<Envelope<JobRow>>("/api/v1/jobs", params),
        getJson<{ data: CompanyMeta[] }>("/api/v1/meta/companies"),
      ]);
      setResult(jobs); setCompanies(companyResult.data);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "暂时无法读取岗位数据"); }
    finally { setLoading(false); }
  }, [channels, companyKeys, query, queryFields, page, pageSize]);
  useEffect(() => { void load(); }, [load]);

  const companyOptions = useMemo(() => companies.map((item) => ({ value: item.key, label: item.name, companyKey: item.key })), [companies]);
  const total = result?.meta.pagination?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(page, pageCount);
  const coverage = result?.meta.coverage;
  function submit(event: FormEvent) { event.preventDefault(); setPage(1); setQuery(draft.trim()); }

  return (
    <>
      <PageHeader eyebrow="岗位查询" title="在招岗位查询" description="按公司、招聘类型和岗位内容查找机会。" actions={<RefreshButton onClick={() => void load()} loading={loading} />} />
      {error && <ErrorNotice message={error} />}
      {coverage && <CoverageNotice completed={coverage.standard_snapshot_count} total={coverage.configured_source_channel_count} />}
      <form className="search-toolbar jobs-toolbar" onSubmit={submit}>
        <SearchField value={draft} onValueChange={setDraft} scopesValue={queryFields} scopes={searchScopes} onScopesChange={(values) => { setQueryFields(values); setPage(1); }} placeholder="搜索岗位，支持间隔关键词" ariaLabel="搜索岗位" />
        <MultiSelectFilter label="公司" options={companyOptions} values={companyKeys} onValuesChange={(values) => { setCompanyKeys(values); setPage(1); }} ariaLabel="筛选公司" />
        <MultiSelectFilter label="招聘类型" options={filterChannelOptions} values={channels} onValuesChange={(values) => { setChannels(values); setPage(1); }} ariaLabel="筛选招聘类型" />
        <button type="submit" className="primary-button"><Search size={15} /><span>搜索</span></button>
      </form>
      <Panel title="岗位列表" note={`${formatNumber(total)} 个岗位${coverage?.snapshot_date ? ` · 更新至 ${coverage.snapshot_date}` : ""}`}>
        {loading && !result ? <LoadingBlock /> : result?.data.length ? <JobTable rows={result.data} onOpen={setSelectedJob} /> : <EmptyState title="没有符合条件的岗位" detail={query && queryFields?.length === 0 ? "请至少选择一个搜索字段。" : "请调整筛选条件或搜索词。"} />}
        <Pagination total={total} page={currentPage} pageSize={pageSize} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} itemLabel="个岗位" />
      </Panel>
      {selectedJob && <JobDetailDialog key={`${selectedJob.source_key}-${selectedJob.external_id}`} job={selectedJob} onClose={() => setSelectedJob(null)} />}
    </>
  );
}

function JobTable({ rows, onOpen }: { rows: JobRow[]; onOpen: (job: JobRow) => void }) {
  return <TableWrap><table className="fit-table jobs-table"><thead><tr><th>岗位</th><th>公司</th><th>招聘类型</th><th>官网更新</th><th>数据更新</th><th><span className="sr-only">官网链接</span></th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.source_key}-${row.external_id}`}><td className="job-title"><button type="button" className="job-title-button" onClick={() => onOpen(row)}>{row.title}</button></td><td><CompanyName companyKey={row.company_key} companyName={row.company_name} /></td><td><ChannelTag channel={row.channel} /></td><td>{row.source_updated_at ? new Date(row.source_updated_at).toLocaleDateString("zh-CN") : "-"}</td><td>{new Date(row.last_seen_at).toLocaleDateString("zh-CN")}</td><td><a className="external-link" href={row.source_url} target="_blank" rel="noreferrer" aria-label={`在官网查看 ${row.title}`}><ExternalLink size={15} /><span>官网</span></a></td></tr>)}</tbody></table></TableWrap>;
}
