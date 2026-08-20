"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { BriefcaseBusiness, Building2, CalendarDays, ExternalLink, GraduationCap, History, Layers3, MapPin, RefreshCw, Users, X } from "lucide-react";

import { CompanyName } from "@/components/CompanyLogo";
import { ChannelTag, ErrorNotice, LoadingBlock } from "@/components/ui";
import { formatNumber, getCachedJson, getJson, getStoredJson, type JobDetail, type JobRow } from "@/lib/api";

export function JobDetailDialog({ job, onClose }: { job: JobRow; onClose: () => void }) {
  const path = `/api/v1/jobs/${encodeURIComponent(job.source_key)}/${encodeURIComponent(job.external_id)}`;
  const [detail, setDetail] = useState<JobDetail | null>(() => getCachedJson<JobDetail>(path));
  const [loading, setLoading] = useState(!detail);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(async () => {
    const cached = getStoredJson<JobDetail>(path);
    if (cached) setDetail(cached);
    setLoading(!cached);
    setError(null);
    try {
      setDetail(await getJson<JobDetail>(path));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "暂时无法读取岗位详情");
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [onClose]);

  return createPortal(
    <div className="job-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <article ref={dialogRef} className="job-dialog" role="dialog" aria-modal="true" aria-labelledby="job-dialog-title">
        <header className="job-dialog-header">
          <div className="job-dialog-heading">
            <div className="job-dialog-company">
              <CompanyName companyKey={job.company_key} companyName={job.company_name} />
              <ChannelTag channel={job.channel} />
            </div>
            <h2 id="job-dialog-title">{job.title}</h2>
            {detail?.source_name && <p>{detail.source_name}</p>}
          </div>
          <div className="job-dialog-actions">
            <a className="primary-button job-official-link" href={detail?.source_url ?? job.source_url} target="_blank" rel="noreferrer">
              <ExternalLink size={15} /><span>前往官网</span>
            </a>
            <button ref={closeRef} type="button" className="icon-button job-dialog-close" onClick={onClose} aria-label="关闭岗位详情" title="关闭">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="job-dialog-body">
          {loading && !detail ? <LoadingBlock /> : error && !detail ? <div className="job-detail-error"><ErrorNotice message={error} /><button type="button" className="icon-text-button" onClick={() => void load()}><RefreshCw size={15} />重试</button></div> : detail && <JobDetailContent detail={detail} />}
        </div>
      </article>
    </div>,
    document.body,
  );
}

function JobDetailContent({ detail }: { detail: JobDetail }) {
  const locationNames = detail.locations.map((location) => location.name);
  const categoryNames = detail.categories.map((category) => category.parent_name ? `${category.parent_name} · ${category.name}` : category.name);
  const businessUnits = detail.business_units.map((unit) => unit.name);
  const experience = formatExperience(detail.experience_min_years, detail.experience_max_years);
  const degree = detail.degree_name;
  const project = detail.recruitment_project_name;
  return (
    <>
      <section className="job-facts" aria-label="岗位基本信息">
        <Fact icon={<MapPin size={16} />} label="工作地点" value={locationNames.length ? locationNames.join("、") : "官网未标注"} />
        <Fact icon={<BriefcaseBusiness size={16} />} label="用工类型" value={detail.employment_type_name || "官网未标注"} />
        <Fact icon={<Layers3 size={16} />} label="岗位类别" value={categoryNames.length ? categoryNames.join("、") : "官网未分类"} />
        <Fact icon={<Building2 size={16} />} label="所属部门" value={detail.department_name || detail.department_code || "官网未标注"} />
        {(degree || experience) && <Fact icon={<GraduationCap size={16} />} label="经验与学历" value={[experience, degree].filter(Boolean).join(" · ")} />}
        {project && <Fact icon={<CalendarDays size={16} />} label="招聘项目" value={project} />}
        {detail.recruitment_count != null && <Fact icon={<Users size={16} />} label="招聘人数" value={`${formatNumber(detail.recruitment_count)} 人`} />}
        {businessUnits.length > 0 && <Fact icon={<Building2 size={16} />} label="业务单元" value={businessUnits.join("、")} />}
      </section>

      <section className="job-copy-section">
        <h3>岗位描述</h3>
        <div className="job-copy">{detail.description?.trim() || "官网暂未提供岗位描述。"}</div>
      </section>
      <section className="job-copy-section">
        <h3>岗位要求</h3>
        <div className="job-copy">{detail.requirements?.trim() || "官网暂未单独提供岗位要求。"}</div>
      </section>

      <section className="job-history-strip" aria-label="岗位更新时间">
        <History size={17} />
        <div><span>官网更新</span><strong>{formatDate(detail.source_updated_at ?? detail.published_at)}</strong></div>
        <div><span>首次收录</span><strong>{formatDate(detail.first_seen_at)}</strong></div>
        <div><span>最近采集</span><strong>{formatDate(detail.last_seen_at)}</strong></div>
        <div><span>内容版本</span><strong>{formatNumber(detail.version_count)}</strong></div>
      </section>
    </>
  );
}

function Fact({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return <div className="job-fact"><span className="job-fact-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong></div></div>;
}

function formatExperience(min: number | null, max: number | null): string | null {
  if (min == null && max == null) return null;
  if (min != null && max != null) return min === max ? `${min} 年经验` : `${min}-${max} 年经验`;
  return min != null ? `${min} 年以上经验` : `${max} 年以内经验`;
}

function formatDate(value: string | null): string {
  if (!value) return "官网未标注";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
}
