"use client";

import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export function Chart({ option, ariaLabel, className = "" }: { option: echarts.EChartsOption; ariaLabel: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const firstRenderRef = useRef(true);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.setOption(
      {
        ...option,
        animation: firstRenderRef.current,
        aria: { enabled: true, decal: { show: true } },
      },
      { notMerge: true, lazyUpdate: true },
    );
    firstRenderRef.current = false;
  }, [option]);

  return <div ref={ref} className={`chart ${className}`} role="img" aria-label={ariaLabel} />;
}
