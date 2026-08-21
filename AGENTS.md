# Job Market Monitor Agent Guide

这是仓库级的通用协作说明。它只保留后续 Agent 必须立即知道的约束；运行环境、部署流程、采集实验和
数据模型等细节见 [`docs/agent-context.md`](docs/agent-context.md)，不要把详细操作手册继续堆到本文件。

## 项目定位

本项目采集各公司官方招聘网站的公开岗位，保存可追溯的历史观测和每日快照，并提供趋势、分类、城市、
公司和岗位分析。当前默认目标环境是本机 Parallels 的 Ubuntu 26.04 虚拟机，生产服务器不属于默认操作范围。

## 必须遵守的约束

- 只把招聘官网直接返回的内容当作来源事实；未经明确模型、版本和证据链支持，不生成 topic、技能、专业或
  其他推测标签。
- 不得为了节省流量或加快速度减少岗位、分页、正文、城市、分类或完整性校验。必须保留来源总数、分页行数、
  唯一岗位集合和 `collection_hash` 等校验。
- 不完整采集不能生成当天权威快照，也不能据此关闭岗位。
- 原始响应、真实岗位数据、数据库备份、`.env`、密钥和实验输出不得提交 Git。
- 不要主动访问、部署、迁移或修改生产服务器；只有用户在当前会话明确授权时才执行生产操作。
- 不得未经用户明确要求停止、重启、删除或重建 Ubuntu 中现有的 PostgreSQL、API、Web 容器及数据库卷。

## 协作方式

- 开始前阅读本文件和相关文档，先执行 `rtk git status --short`，保留已有未提交修改，不擅自回滚。
- 所有 shell 命令使用 `rtk` 前缀；文件编辑使用 `apply_patch`。
- 修改 `frontend/` 前阅读 [`frontend/AGENTS.md`](frontend/AGENTS.md) 及其适用的 Next.js 规则。
- 采集逻辑修改必须先本地测试，再在 Ubuntu Docker 中进行 dry-run/真实实验；不要把测试数据说成真实全量数据。
- 完成改动后按风险运行测试、Ruff、Compose 配置检查和相关构建，并在提交或交付说明中报告未完成风险。

## 构建防误操作

- 日常采集、API/Web 运行和代码层更新不得执行 `docker compose build collector`，也不得在定时任务中
  使用 `--build`、默认拉取或安装 Chromium。两个 Compose 文件中的 `collector` 是镜像-only 服务；
  镜像缺失应直接失败。
- 采集器仅通过 `deploy/build-collector-offline.sh` 做代码层更新。该脚本要求本地已验证的
  `job-market-monitor-collector:vm-base`，并强制 Docker `--pull=false --network=none`；基础镜像不存在
  时不要改为在线构建。
- 只有 Playwright 或 Chromium 版本确实需要升级时，才允许直接构建根目录 `Dockerfile`，且必须显式传入
  `--build-arg ALLOW_NETWORK_BUILD=1`。这是一次性的发布操作，不是每日采集步骤；构建前后要单独记录
  镜像大小、依赖版本和网络流量。
- 新会话开始涉及 Docker 前，先阅读本节、`docs/deployment.md` 和 `docs/agent-context.md`，检查本地
  镜像标签；不要把“构建成功”当作可以自动下载依赖的授权。
- API 和 Web 的日常代码更新分别使用 `deploy/build-api-offline.sh` 与
  `deploy/build-web-offline.sh`。两者都要求已验证的本地基础镜像并强制断网构建；Web 脚本还要求先在
  当前工作区完成 `frontend` 生产构建。只有依赖版本确实变化时才允许使用原始在线 Dockerfile。

## 入口文档

- [运行与工程上下文](docs/agent-context.md)：Ubuntu、systemd、容器、并发、流量和数据模型细节
- [Ubuntu 连续试运行](docs/ubuntu-trial.md)：虚拟机常驻服务、定时任务和三日验收
- [部署说明](docs/deployment.md)：本地 Compose 与生产外部 PostgreSQL 的边界
- [并发与流量实验](docs/concurrency-and-bandwidth.md)：实验方法、指标口径和来源优化结果
- [数据契约](docs/data-contract.md)：来源事实、派生字段、地点和快照语义
- [数据源目录](docs/source-catalog.md)：官方招聘入口、渠道边界和已知限制
