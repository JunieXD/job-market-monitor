import type { SimpleIcon } from "simple-icons";
import {
  siAlibabacloud,
  siAlibabadotcom,
  siAlipay,
  siBaidu,
  siBilibili,
  siBytedance,
  siHuawei,
  siKuaishou,
  siLenovo,
  siMeituan,
  siOppo,
  siTripdotcom,
  siVivo,
  siXiaohongshu,
  siXiaomi,
} from "simple-icons";

type CompanyMark = {
  icon?: SimpleIcon;
  color: string;
  monogram?: string;
};

const companyMarks: Record<string, CompanyMark> = {
  qihu360: { color: "#19a451", monogram: "360" },
  oppo: { color: `#${siOppo.hex}`, icon: siOppo },
  vivo: { color: `#${siVivo.hex}`, icon: siVivo },
  jd: { color: "#e1251b", monogram: "JD" },
  huawei: { color: `#${siHuawei.hex}`, icon: siHuawei },
  tongcheng: { color: "#f5a623", monogram: "同" },
  bilibili: { color: `#${siBilibili.hex}`, icon: siBilibili },
  bytedance: { color: `#${siBytedance.hex}`, icon: siBytedance },
  xiaomi: { color: `#${siXiaomi.hex}`, icon: siXiaomi },
  xiaohongshu: { color: `#${siXiaohongshu.hex}`, icon: siXiaohongshu },
  kuaishou: { color: `#${siKuaishou.hex}`, icon: siKuaishou },
  pdd: { color: "#e02e24", monogram: "拼" },
  ctrip: { color: `#${siTripdotcom.hex}`, icon: siTripdotcom },
  didi: { color: "#ff7d41", monogram: "滴" },
  iqiyi: { color: "#00be06", monogram: "爱" },
  baidu: { color: `#${siBaidu.hex}`, icon: siBaidu },
  netease: { color: "#d43c33", monogram: "易" },
  meituan: { color: "#c28e00", icon: siMeituan },
  lenovo: { color: `#${siLenovo.hex}`, icon: siLenovo },
  tencent: { color: "#1769aa", monogram: "腾" },
  cainiao: { color: "#2354e6", monogram: "菜" },
  ant: { color: `#${siAlipay.hex}`, icon: siAlipay },
  beike: { color: "#00ae66", monogram: "贝" },
  alibaba_cloud: { color: `#${siAlibabacloud.hex}`, icon: siAlibabacloud },
  alibaba_international: { color: `#${siAlibabadotcom.hex}`, icon: siAlibabadotcom },
  alibaba: { color: `#${siAlibabadotcom.hex}`, icon: siAlibabadotcom },
};

export function CompanyLogo({ companyKey, companyName }: { companyKey: string; companyName: string }) {
  const mark = companyMarks[companyKey] ?? {
    color: "#5d6972",
    monogram: companyName.trim().slice(0, 1).toUpperCase(),
  };
  return (
    <span className="company-logo" aria-hidden="true">
      {mark.icon ? (
        <svg viewBox="0 0 24 24" focusable="false" style={{ color: mark.color }}>
          <path fill="currentColor" d={mark.icon.path} />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" focusable="false">
          <rect width="24" height="24" rx="5" fill={mark.color} />
          <text x="12" y="15.4" textAnchor="middle" fill="#fff" fontSize={mark.monogram?.length === 3 ? "7" : "9.5"} fontWeight="700">
            {mark.monogram}
          </text>
        </svg>
      )}
    </span>
  );
}

export function CompanyName({ companyKey, companyName, className = "" }: { companyKey: string; companyName: string; className?: string }) {
  return (
    <span className={`company-name ${className}`}>
      <CompanyLogo companyKey={companyKey} companyName={companyName} />
      <span>{companyName}</span>
    </span>
  );
}
