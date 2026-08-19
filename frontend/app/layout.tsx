import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "就业市场监测器",
  description: "官方招聘岗位趋势、城市分布和来源质量看板。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
