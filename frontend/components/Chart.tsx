"use client";

import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export function Chart({ option, ariaLabel, className = "" }: { option: echarts.EChartsOption; ariaLabel: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    chart.setOption({ ...option, aria: { enabled: true, decal: { show: true } } });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [option]);

  return <div ref={ref} className={`chart ${className}`} role="img" aria-label={ariaLabel} />;
}
