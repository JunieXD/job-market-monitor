export const channelOptions = [
  { value: "all", label: "全部招聘类型" },
  { value: "experienced", label: "社会招聘" },
  { value: "campus", label: "校园招聘" },
  { value: "internship", label: "实习招聘" },
  { value: "general", label: "综合招聘" },
];

const channelLabels: Record<string, string> = {
  experienced: "社会招聘",
  campus: "校园招聘",
  internship: "实习招聘",
  general: "综合招聘",
};

const collectionStateLabels: Record<string, string> = {
  completed: "采集完成",
  running: "正在采集",
  failed: "采集失败",
  partial: "结果不完整",
  pending: "等待采集",
};

export function channelLabel(channel: string): string {
  return channelLabels[channel] ?? "其他招聘";
}

export function collectionStateLabel(state: string): string {
  return collectionStateLabels[state] ?? "状态未知";
}
