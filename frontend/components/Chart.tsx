"use client";

import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export function Chart({ option, ariaLabel }: { option: echarts.EChartsOption; ariaLabel: string }) {
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

  return <div ref={ref} className="chart" role="img" aria-label={ariaLabel} />;
}
