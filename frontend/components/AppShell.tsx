"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  BarChart3,
  BriefcaseBusiness,
  ChartNoAxesCombined,
  DatabaseZap,
  LayoutDashboard,
  MapPinned,
} from "lucide-react";

import { prefetchJson } from "@/lib/api";

const navigation = [
  { href: "/", label: "总览", icon: LayoutDashboard },
  { href: "/trends", label: "趋势", icon: ChartNoAxesCombined },
  { href: "/categories", label: "岗位分类", icon: BarChart3 },
  { href: "/cities", label: "城市", icon: MapPinned },
  { href: "/jobs", label: "岗位", icon: BriefcaseBusiness },
  { href: "/collection", label: "采集状态", icon: DatabaseZap },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="app-shell">
      <header className="site-header">
        <div className="header-inner">
          <Link href="/" className="brand-link" aria-label="返回就业市场监测器总览">
            <span className="brand-mark" aria-hidden="true"><Activity size={20} /></span>
            <span className="brand-copy"><strong>就业市场监测器</strong><small>官方招聘岗位数据</small></span>
          </Link>
          <nav className="main-nav" aria-label="主要页面">
            {navigation.map(({ href, label, icon: Icon }) => {
              const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
              return (
                <Link
                  key={href}
                  href={href}
                  prefetch
                  className={`nav-link ${active ? "active" : ""}`}
                  onMouseEnter={() => prefetchRouteData(href)}
                  onFocus={() => prefetchRouteData(href)}
                  onPointerDown={() => prefetchRouteData(href)}
                >
                  <Icon size={16} />
                  <span>{label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
      </header>
      <main className="page-container">{children}</main>
      <footer className="site-footer">数据来自企业公开招聘官网；岗位数量不等于实际招聘人数。</footer>
    </div>
  );
}

function prefetchRouteData(href: string) {
  const requestsByRoute: Record<string, Array<[string, Record<string, string | number> | undefined]>> = {
    "/": [["/api/v1/overview", undefined], ["/api/v1/trends/market", undefined]],
    "/trends": [["/api/v1/trends/companies", undefined]],
    "/categories": [["/api/v1/distributions/categories", undefined]],
    "/cities": [["/api/v1/distributions/cities", undefined], ["/api/v1/meta/companies", undefined]],
    "/jobs": [["/api/v1/jobs", { limit: 20, offset: 0 }], ["/api/v1/meta/companies", undefined]],
    "/collection": [["/api/v1/collection/status", undefined]],
  };
  const requests = requestsByRoute[href] ?? [];
  requests.forEach(([path, params]) => { void prefetchJson(path, params); });
}
