# 网站、API 与分析页面实施计划

## 1. 目标

在不改变现有采集器和事实数据边界的前提下，为就业市场监测器增加一个只读分析 API 和网站 MVP，
让用户可以稳定查看岗位趋势、公司对比、分类分布、城市分布、岗位明细和数据质量。

当前及后续所有实施、构建和联调均以 Ubuntu Parallels 虚拟机为测试环境；生产服务器和生产
PostgreSQL 在本计划完成前不连接、不迁移、不部署。

## 2. 范围

### 本阶段包含

- 固化分析指标和 API 响应契约。
- 在现有 Python 项目中增加 FastAPI 只读 API。
- 复用现有 PostgreSQL、岗位版本、每日快照和生命周期模型。
- 增加公司、来源、渠道、日期、分类、城市和岗位查询接口。
- 增加网站 MVP：市场总览、趋势、公司对比、分类、城市、岗位浏览器和数据质量页面。
- 使用 Docker Compose 在 Ubuntu 无图形界面环境中运行 API、前端和采集器。
- 增加 API、查询、前端构建和容器健康检查。

### 明确不包含

- 不新建生产 PostgreSQL，不连接用户生产服务器。
- 不在第一版引入 Redis、ClickHouse、Elasticsearch、GraphQL 或微服务拆分。
- 不启用未经确认的 topic、技能、专业或 LLM 派生标签。
- 不把官网原始 HTML、Cookie、账号信息或真实批量岗位数据放入 GitHub。
- 不把不完整或不具备缺失判断权威性的采集结果伪装成完整市场趋势。

## 3. 技术方案

### 后端 API

- FastAPI：HTTP 路由、参数校验和 OpenAPI 文档。
- Pydantic v2：请求和响应模型。
- SQLAlchemy 2：复用现有数据库连接、模型和事务边界。
- PostgreSQL 16：继续使用当前数据库，不引入新的事实存储。
- REST `/api/v1`：第一版优先可读、可缓存、易调试，不引入 GraphQL。

API 是只读服务，不执行采集、不修改岗位事实、不接收任意 SQL。岗位原始 JSON 默认不通过公开 API
返回，只返回规范化字段和官网详情链接。

### 前端网站

- Next.js、React、TypeScript：构建网站和分析看板。
- Apache ECharts：趋势、堆叠柱状图、热力图和后续地图。
- 浏览器原生 `fetch` 与 React 状态：请求、加载、错误和筛选条件联动。
- Radix UI Select：可访问的自定义选择器；其余样式使用项目统一 CSS 变量与复用组件。

网站第一屏直接进入看板，不制作营销型 Landing Page。地图作为后续增强，第一版先使用排行、矩阵
和趋势图，避免引入未经验证的地图边界数据。

### 部署

- API、前端使用独立 Docker service。
- Ubuntu 上由 Docker Compose 运行；采集器仍由现有 systemd 调度。
- 生产 Compose 只加入已有 PostgreSQL Docker network，不创建 PostgreSQL。
- 反向代理优先复用服务器已有 Nginx/Caddy，避免端口和证书管理冲突。

## 4. 分析口径与数据约束

1. 日度趋势只使用 `daily_snapshots` 指向的标准快照，不用 `jobs` 当前状态拼接历史。
2. 每个结果返回 `snapshot_date`、时区、来源覆盖数、成功数和 `absence_authoritative` 状态。
3. 不完整或非权威采集只能作为观测或质量信息，不推进岗位关闭，也不能默认为完整趋势。
4. 岗位条目数不等于招聘人数，页面必须分别命名和展示。
5. 来源分类、统一分类、未分类和未映射必须分开。
6. 多分类岗位可能导致分类占比总和超过 100%，页面必须展示分类赋值方法。
7. 多城市岗位同时提供关联岗位数和折算岗位数两种口径。
8. 历史查询不能因为当前分类或城市映射变化而重写过去；查询必须使用与快照日期相匹配的映射版本，
   或使用已经在每日聚合中固化的结果。
9. 业务单元只按来源事实统计，不能跨公司或跨来源自动合并。
10. 主题、技能、专业、学历推断等派生结果不进入第一版默认接口。

## 5. API 初版契约

### 元数据

```text
GET /api/v1/meta/companies
GET /api/v1/meta/sources
GET /api/v1/meta/channels
GET /api/v1/meta/categories
GET /api/v1/meta/locations
```

### 分析

```text
GET /api/v1/overview
GET /api/v1/trends/market
GET /api/v1/trends/companies
GET /api/v1/distributions/categories
GET /api/v1/distributions/cities
GET /api/v1/companies/{company_key}/summary
```

### 岗位与质量

```text
GET /api/v1/jobs
GET /api/v1/jobs/{source_key}/{external_id}
GET /api/v1/quality/source-health
GET /api/v1/quality/coverage
GET /api/v1/collection/status
```

所有分析接口统一返回：

```json
{
  "data": [],
  "meta": {
    "snapshot_date": "2026-08-19",
    "timezone": "Asia/Shanghai",
    "filters": {},
    "coverage": {
      "source_count": 29,
      "successful_source_count": 26,
      "absence_authoritative_source_count": 24
    },
    "metric_definition": "active_posting_count"
  }
}
```

岗位列表必须分页，日期范围、排序字段和单页大小必须有上限；所有筛选条件进入结构化参数，不能拼接
SQL。错误响应包含稳定错误码和可读说明，不能泄露数据库连接信息或内部堆栈。

## 6. 页面和验收标准

### 阶段一：分析契约和查询层

交付物：

- API Pydantic schema 和指标定义。
- 现有分析视图中未分类、未映射、覆盖率和历史映射版本问题的处理方案。
- 查询函数和测试 fixture。

验收标准：

- 每个核心指标都有明确 SQL 来源、分母和时间口径。
- 给定固定 fixture 时，公司趋势、分类占比和城市折算占比结果稳定可重现。
- 历史映射变化不会改变旧快照的分析结果。
- 未分类与未映射不会被静默丢弃。

### 阶段二：FastAPI MVP

交付物：

- `/api/v1` 路由、响应模型、数据库依赖和 OpenAPI 文档。
- API Dockerfile/Compose service。
- API 单元测试和数据库集成测试。

验收标准：

- API 能在 Ubuntu Docker 中启动并通过 `/healthz`。
- 核心接口返回统一 `data/meta` 结构。
- 无参数请求不会扫描无限日期或返回无限岗位。
- API 只读数据库，岗位详情可以追溯到来源 URL。
- PostgreSQL 集成测试和现有采集器测试全部通过。

### 阶段三：网站 MVP

交付页面：

1. 市场总览：活跃、新增、关闭、变化岗位及数据覆盖状态。
2. 趋势分析：市场和公司按日期的岗位变化。
3. 岗位分类：排行、占比和映射状态。
4. 城市分布：关联岗位数、折算岗位数和城市份额。
5. 岗位浏览器：公司、招聘类型、关键词筛选、分页和官网链接。
6. 采集状态：当天完成进度、来源状态、失败摘要和下一次计划时间。

验收标准：

- 桌面和移动窄屏下不发生文本、图表和筛选控件重叠。
- 每张图表都显示日期、来源覆盖状态和指标口径。
- 加载、空数据、部分覆盖、接口错误和重试状态完整可见。
- 图表和表格使用同一 API 数据，筛选条件不会互相漂移。
- 页面不显示未经发布的 topic 或 LLM 推断结果。

### 阶段四：Ubuntu 联调和部署验证

交付物：

- Ubuntu Docker Compose 配置。
- API、前端和采集器之间的网络与环境变量说明。
- systemd 调度与网站服务的日志检查步骤。

验收标准：

- Ubuntu 无图形界面环境可以构建并启动 API 和前端。
- API 能读取现有测试 PostgreSQL，并正确返回 fixture 或测试采集数据。
- 采集器失败不会导致网站容器崩溃；网站能显示来源质量状态。
- 无遗留临时容器，原始数据和凭据仍在 Git 忽略范围外。
- 生产服务器未被访问，所有验证记录来自 Ubuntu 虚拟机。

## 7. 提交和发布规则

- 每完成一个可验收阶段创建一个独立 Git commit。
- commit 前至少运行 `pytest`、`ruff`、shell 语法检查和对应 Docker 配置检查。
- API 和前端新增依赖必须锁定版本并在 CI 中构建。
- 采集器、数据库模型、API 和前端的大型改动不要混在一个无法回滚的提交中。
- GitHub 只提交代码、文档、测试和脱敏 fixture；真实采集数据留在 Ubuntu named volume。
- 没有通过阶段验收，不进入生产部署。

## 8. 风险与处理

| 风险 | 处理方式 |
| --- | --- |
| 部分来源当天不完整 | API 返回覆盖率和权威性；页面明确标记，不伪造全市场趋势 |
| 统一分类或城市映射变化 | 使用版本化映射或每日固化聚合结果 |
| 聚合视图查询变慢 | 先加索引和日期限制，数据量增长后改为每日物化聚合表 |
| 原始岗位数据过大 | API 不返回原始 JSON，保留来源链接和规范化字段 |
| 服务器已有容器和端口 | 使用独立 Compose service，复用已有 network 和反向代理 |
| 前端图表误读 | 指标说明、空状态、覆盖状态和口径信息与图表同时展示 |

## 9. 当前执行目标

阶段一至阶段四已经完成，并已在 Ubuntu Parallels 虚拟机中通过 Docker 联调。当前进入连续真实
采集试运行，验收方式见 [Ubuntu 连续试运行](ubuntu-trial.md)；生产服务器仍保持不变。
