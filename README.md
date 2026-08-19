# 就业市场监测器

[![CI](https://github.com/JunieXD/job-market-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/JunieXD/job-market-monitor/actions/workflows/ci.yml)
[![许可证：MIT](https://img.shields.io/badge/许可证-MIT-green.svg)](LICENSE)

一个面向求职者的开源就业市场数据采集与趋势分析基础设施。

项目从各家公司公开的招聘官网采集岗位事实，记录岗位的出现、内容变化、缺失和重新开放，
为回答“哪些城市岗位更集中”“某类岗位何时开始增加”“不同公司的岗位结构如何变化”等问题，
提供可追溯的数据基础。

> 当前项目处于采集器和数据层建设阶段，尚未提供可直接访问的在线分析网站，也不会把未经验证的
> 主题标签或 LLM 结论混入默认数据。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 官方招聘来源 | 26 家公司、29 个独立来源、41 个来源渠道 |
| 采集方式 | Playwright 无界面 Chromium，读取招聘页面自身发出的公开 JSON 响应 |
| 数据库 | PostgreSQL 16 |
| 原始数据 | gzip JSON，保存在 Docker named volume，不进入 Git |
| 定时运行 | Ubuntu systemd timer，每天按上海时间运行 |
| 测试 | 160 passed，1 skipped |
| 当前阶段 | 数据采集、数据契约和部署验证；前端和生产部署待后续进行 |

## 已接入来源

当前注册表覆盖字节、阿里各业务招聘站、腾讯、美团、京东、网易、小米、华为、快手、百度、
滴滴、哔哩哔哩、拼多多、蚂蚁、携程、贝壳、联想、360、OPPO、vivo、小红书、爱奇艺和菜鸟等
官方招聘入口。

完整范围、渠道边界、已知限制和来源统计口径见
[数据源目录](docs/source-catalog.md)。也可以直接运行：

```bash
uv run job-market list-sources --format summary
uv run job-market list-sources --format json
```

不同招聘站不会被未经审计地简单相加。例如，阿里集团统一校园入口只代表校园招聘；阿里云、淘天、
菜鸟和阿里国际分别作为独立来源保存。来源岗位数、公司去重后岗位数和招聘人数是不同指标，报告中
必须同时记录来源、渠道和快照日期。

## 数据原则

项目把数据分为四层，避免把推测伪装成官网事实：

1. **来源事实**：职位 ID、标题、正文、招聘渠道、官网分类、城市、发布时间、学历代码、经验区间、
   部门和招聘人数等，仅保存官网直接提供的内容。
2. **系统观测**：某次采集看到了哪个岗位、使用了哪个内容版本、抓取是否完整以及原始响应证据。
3. **统一维度**：跨来源的城市和分类映射，使用版本化映射和置信度，来源原文不会被覆盖。
4. **派生结果**：未来的规则、人工标注或 LLM 提取结果，必须关联岗位版本、方法、证据和置信度；
   当前默认停用。

当前不会从职位描述自行生成 topic、技能、专业或岗位类别。官网直接返回的业务单元（例如阿里
`circleCodeList`/`circleNames`）只作为来源事实维度保存，不等同于项目自定义标签。

## 数据模型能支持什么

数据库不是一张“当前岗位表”，而是保留历史和证据的观测模型：

- `jobs`：岗位在某个来源内的稳定身份。
- `job_versions`：岗位内容的每个版本。
- `job_observations`：某次采集对某个版本的观测。
- `job_version_locations`：岗位版本对应的一个或多个城市。
- `job_version_source_categories`：官网直接分类或官网筛选成员关系。
- `daily_snapshots`：来源和渠道的每日标准快照。
- `job_lifecycle_events`：首次出现、变更、缺失、关闭、恢复和重新开放。
- `raw_snapshots`、`crawl_run_field_stats`：原始 JSON 证据和字段覆盖率。

因此后续可以重建以下分析，而不是只看一次采集的静态数量：

- 公司、来源、渠道和城市的岗位数量趋势。
- 某个官网分类或岗位关键词的首次出现与每日变化。
- 城市岗位分布、公司在不同城市的结构差异。
- 岗位新增、关闭、重新开放和内容变化。
- 多城市岗位的来源级、公司级和去重后统计。

## 快速开始

### 环境要求

- Python 3.12 至 3.14
- Docker Engine 和 Docker Compose
- PostgreSQL 16（本地 Compose 可启动测试数据库；生产环境可接入已有 PostgreSQL）
- 能够访问目标招聘官网的网络环境

### 安装依赖

```bash
uv sync --extra dev
uv run playwright install --with-deps chromium
```

也可以使用 Docker 构建采集镜像：

```bash
docker compose build collector
```

### 配置本地环境

```bash
cp .env.example .env
docker compose up -d postgres
```

`.env` 只保存在本地，不要提交。数据库连接、原始数据目录、请求间隔和超时参数都可以通过环境变量
调整，具体示例见 [.env.example](.env.example)。

### 初始化数据库并运行一次 dry-run

dry-run 会访问官网并执行解析、分页和完整性校验，但不会写数据库或原始数据：

```bash
docker compose run --rm collector init-db
docker compose run --rm collector crawl \
  --source bytedance \
  --channel campus \
  --dry-run \
  --max-pages 1
```

确认结果后，再去掉 `--dry-run` 执行实际入库。单来源运行示例：

```bash
docker compose run --rm collector crawl \
  --source didi \
  --channel experienced
```

常用检查命令：

```bash
docker compose run --rm collector check-schema
docker compose run --rm collector check-data
docker compose run --rm collector check-runtime
docker compose run --rm collector check-source-health
```

### Ubuntu 无图形界面定时采集

采集器默认以 headless Chromium 运行，不需要桌面环境、物理显示器、X11、VNC 或单独配置 Xvfb。

批量调度脚本会为每个来源创建独立容器，单个来源失败不会阻止后续来源，并在批次结束时统一汇总：

```bash
sudo install -m 0755 deploy/run-scheduled-crawls.sh /opt/job-market-monitor/deploy/
sudo install -m 0644 deploy/systemd/job-market-crawl.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/job-market-crawl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-market-crawl.timer
systemctl list-timers job-market-crawl.timer
```

生产 Compose 不会创建 PostgreSQL，而是加入已有的外部 Docker network。部署前需要填写
`DATABASE_URL` 和 `DATABASE_DOCKER_NETWORK`，完整说明见[部署说明](docs/deployment.md)。

### 启动只读分析 API

本地 Compose 会同时提供 API，默认监听 `127.0.0.1:8000`：

```bash
docker compose up -d postgres api web
curl http://127.0.0.1:8000/healthz
```

网站默认监听 `http://127.0.0.1:3000`，前端通过同源路径访问 API，不需要在浏览器中配置跨域地址。

接口文档由 FastAPI 自动生成：

```text
http://127.0.0.1:8000/docs
```

当前 API 提供市场总览、公司趋势、分类分布、城市分布、岗位分页、岗位详情和来源健康检查。所有
分析响应都带有快照日期、指标口径和来源覆盖状态；API 只读数据库，不执行采集或修改岗位事实。

## 目录结构

```text
src/job_market/connectors/   各招聘官网连接器
src/job_market/schemas.py    来源事实和采集结果契约
src/job_market/models.py     SQLAlchemy 数据模型
src/job_market/repository.py 采集结果入库与生命周期处理
src/job_market/analytics.py  可重建的分析查询
src/job_market/migrations/   Alembic 数据库迁移
tests/                       解析、模型、质量和命令测试
deploy/                      Docker 调度脚本和 systemd 配置
docs/                        数据契约、来源目录和指标定义
```

## 开发与贡献

```bash
uv run ruff check .
uv run pytest -q
bash -n deploy/run-scheduled-crawls.sh
```

欢迎贡献新的官方招聘来源、解析测试、数据质量规则和分析查询。连接器只能访问公开且无需登录的
招聘页面；禁止提交真实批量岗位数据、原始网页、Cookie、账号信息、数据库文件或数据库导出。
详细要求见 [贡献指南](CONTRIBUTING.md)。

## 访问边界与隐私

本项目只读取招聘官网公开展示的信息，并遵守合理请求间隔和来源自身的访问边界。不使用个人登录态、
验证码绕过、代理池、指纹伪装或其他访问控制绕过手段。各来源的限制和未接入原因记录在
[数据源目录](docs/source-catalog.md) 中。

项目不收集求职者个人信息，也不提供招聘决策或录用预测。数据仅用于就业市场的公开信息观测，使用者
仍应以招聘官网最新页面和公司正式通知为准。

## 文档

| 文档 | 内容 |
| --- | --- |
| [数据契约](docs/data-contract.md) | 来源事实、版本、观测、快照、分类、城市和派生层规则 |
| [数据源目录](docs/source-catalog.md) | 已接入官网、渠道范围、计数边界和已知限制 |
| [指标定义](docs/metrics.md) | 岗位趋势、生命周期和公司/来源统计口径 |
| [部署说明](docs/deployment.md) | 本地 Ubuntu 验证、已有 PostgreSQL 和 systemd 定时任务 |
| [网站与 API 实施计划](docs/web-api-plan.md) | 页面、接口、验收标准和分阶段提交目标 |
| [贡献指南](CONTRIBUTING.md) | 新增连接器、fixture、测试和数据安全要求 |
| [安全策略](SECURITY.md) | 安全漏洞报告和运行数据边界 |
| [生产 Compose](compose.production.yaml) | 接入已有 PostgreSQL 的生产容器配置 |

## 开源许可证

[MIT License](LICENSE)。

## 项目简介

用于 GitHub 仓库描述的简短版本：

> 面向求职者的开源就业市场监测器：从官方招聘网站采集岗位事实，追踪岗位趋势、城市分布和生命周期变化。
