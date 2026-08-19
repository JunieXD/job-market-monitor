import { AlertTriangle, RefreshCw } from "lucide-react";

import { channelLabel } from "@/lib/labels";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{description}</p></div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Panel({
  title,
  note,
  children,
  className = "",
  action,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
  className?: string;
  action?: React.ReactNode;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-head">
        <div><h2>{title}</h2>{note && <p>{note}</p>}</div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function MetricCard({
  label,
  value,
  detail,
  icon,
  tone = "teal",
}: {
  label: string;
  value: string;
  detail: string;
  icon: React.ReactNode;
  tone?: "teal" | "amber" | "coral" | "blue";
}) {
  return (
    <article className={`metric-card ${tone}`}>
      <div className="metric-label"><span>{label}</span>{icon}</div>
      <strong>{value}</strong>
      <p>{detail}</p>
    </article>
  );
}

export function CoverageNotice({ completed, total }: { completed: number; total: number }) {
  if (total > 0 && completed >= total) return null;
  return (
    <div className="notice" role="status">
      <AlertTriangle size={17} />
      <span>今日数据尚未全部更新（{completed} / {total}），当前结果基于已完成数据。</span>
    </div>
  );
}

export function ErrorNotice({ message }: { message: string }) {
  return <div className="notice error" role="alert"><AlertTriangle size={17} /><span>{message}</span></div>;
}

export function RefreshButton({ onClick, loading = false }: { onClick: () => void; loading?: boolean }) {
  return (
    <button className="icon-text-button" type="button" onClick={onClick} disabled={loading}>
      <RefreshCw size={15} className={loading ? "spin" : ""} />
      <span>刷新</span>
    </button>
  );
}

export function LoadingBlock() {
  return <div className="state-block" aria-label="正在加载"><div><span className="skeleton wide" /><span className="skeleton" /></div></div>;
}

export function EmptyState({ title, detail = "当前条件下暂无数据。" }: { title: string; detail?: string }) {
  return <div className="state-block"><div><strong>{title}</strong><span>{detail}</span></div></div>;
}

export function ChannelTag({ channel }: { channel: string }) {
  return <span className="tag channel-tag">{channelLabel(channel)}</span>;
}

export function TableWrap({ children }: { children: React.ReactNode }) {
  return <div className="table-wrap">{children}</div>;
}
