# Job Market Monitor 运行与工程上下文

这是一份给后续 Codex/Agent 会话查阅的详细上下文。它记录运行环境、部署方式、采集实验、数据模型和
验收要求；根目录的 [`AGENTS.md`](../AGENTS.md) 只保留通用约束和入口说明。修改 `frontend/` 时还必须
阅读 [`frontend/AGENTS.md`](../frontend/AGENTS.md)。

## 项目目标

本项目从各大互联网公司的官方招聘网站采集公开岗位，保存可追溯的岗位观测和每日快照，提供面向求职者的
趋势、分类、城市、公司和岗位分析。项目当前仍处于真实采集试运行阶段，优先保证数据完整、来源可追溯和
失败可定位，再优化速度和流量。

默认原则：

- 只把招聘官网直接返回的内容保存为来源事实；没有官网直接提供的字段先保持为空。
- 不在没有模型和版本化证据链的情况下生成 topic、技能、专业、学历或其他推测标签。
- 不能为了省流量减少分页、岗位、城市、分类或正文；必须保留官网总数、分页行数、唯一岗位集合和
  `collection_hash` 校验。
- 未完整完成的采集可以保存观测和问题，但不能生成当天权威 `daily_snapshots`，也不能据此关闭岗位。
- 真实岗位数据、原始响应、数据库备份、`.env` 和任何密钥不进入 Git。

## 当前运行环境

### Ubuntu Parallels 虚拟机

所有当前部署、真实采集和网站联调先在本机 Parallels 虚拟机完成，不要默认连接生产服务器。

- 虚拟机名称：`Ubuntu 26.04`
- 资源基线：2 vCPU，约 3.3 GiB 内存
- 采集器在 Docker 内使用 headless Chromium，不依赖宿主机桌面、X11、VNC 或显示器
- 宿主机共享目录通常挂载为 `/media/psf/job-market-monitor`，用于传递代码和构建上下文；它可能是只读或
  性能较差的 Parallels 共享文件系统，不应作为长期运行目录
- 推荐在虚拟机可写目录中运行发布副本：`/opt/job-market-monitor` 或已配置的发布目录

不要假设 systemd 当前使用哪个目录。虚拟机曾配置过发布目录 drop-in，实际路径可能是
`/opt/job-market-monitor-release`。每次部署或排查前先执行：

```bash
rtk proxy prlctl exec "Ubuntu 26.04" systemctl show job-market-crawl.service \
  -p WorkingDirectory -p ExecStart -p Environment
```

当前虚拟机中的常驻服务包括 API、Web 和 PostgreSQL。采集器是定时任务按来源创建的临时容器。
未经用户明确要求，不要停止、重启、删除或重建现有 `postgres`、`api`、`web` 容器和 PostgreSQL 数据卷。

### 生产环境边界

仓库准备公开到 GitHub。生产服务器和生产 PostgreSQL 不属于默认测试范围；不要主动 SSH、部署、迁移或
修改生产环境。生产 Compose 使用已有 PostgreSQL 所在的外部 Docker network，不创建新的 PostgreSQL。
只有用户在当前会话明确授权时，才可以执行生产操作，并且必须先做只读检查。

## 虚拟机服务与采集调度

- 定时器：`job-market-crawl.timer`
- 服务：`job-market-crawl.service`
- 默认计划：每天上海时间 `03:15`，允许持久化触发和最多 5 分钟随机延迟
- 批量脚本：`deploy/run-scheduled-crawls.sh`
- 默认 Compose：虚拟机测试使用 `compose.yaml`；生产使用 `compose.production.yaml`
- 默认来源并发：`MAX_PARALLEL_SOURCES=2`
- 默认来源启动间隔：`SOURCE_START_DELAY_SECONDS=3`
- 每个来源独立容器、超时、重试、清理和结果汇总；单个来源失败不能阻止其他来源
- `flock` 防止两个批次重叠；`--due-only` 避免同一天重复采集已完成来源
- 日志：journald 和 `/var/log/job-market-monitor/crawl.jsonl`
- 日志轮转：单文件 20 MB，压缩日志保留 14 天；Docker `json-file` 日志每容器最多 3 x 10 MB

常用只读检查：

```bash
rtk proxy prlctl exec "Ubuntu 26.04" systemctl is-active job-market-crawl.timer
rtk proxy prlctl exec "Ubuntu 26.04" systemctl list-timers job-market-crawl.timer --no-pager
rtk proxy prlctl exec "Ubuntu 26.04" journalctl -u job-market-crawl.service -n 200 --no-pager
rtk proxy prlctl exec "Ubuntu 26.04" tail -n 100 /var/log/job-market-monitor/crawl.jsonl
```

网站和 API 常用入口（以虚拟机端口转发/IP 为准）：

- 网站：`http://<Ubuntu虚拟机IP>:3000`
- 采集状态：`http://<Ubuntu虚拟机IP>:3000/collection`
- API：`http://<Ubuntu虚拟机IP>:8000`
- API 文档：`http://<Ubuntu虚拟机IP>:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`

## 采集器与流量规则

代码入口和来源注册表：

- CLI 与来源注册：`src/job_market/cli.py`
- 来源连接器：`src/job_market/connectors/`
- 数据库入库：`src/job_market/repository.py`
- 浏览器网络指标：`src/job_market/browser_network.py`
- 定时批次：`deploy/run-scheduled-crawls.sh`
- 并发/流量实验：`deploy/run-concurrency-benchmark.sh`
- 实验记录：`docs/concurrency-and-bandwidth.md`

连接器应优先使用招聘官网页面自身发出的公开 JSON 接口，保留必要的官网字段和原始响应。常见图片、
字体、音视频和 Service Worker 默认通过 CDP 资源策略阻止；不要用会关闭 Chromium HTTP cache 的全局
`context.route`。来源若依赖初始化图片或脚本，必须在 `cli.py` 的来源规格中显式关闭资源阻止。

网络指标有三个不同口径，不能相加：

1. CDP `received_bytes`：近似的浏览器响应体字节数；APIRequestContext 直连接口可能不进入该统计。
2. Docker `NET I/O`：来源容器实际收发量，作为项目网络账单的主要口径。
3. 虚拟机网卡或宿主机 FlowWatch：包含协议开销、Docker/Parallels 转发和虚拟机其他流量；不能把
   FlowWatch 数字再与 Docker 数字相加。

构建镜像、下载 Chromium、Parallels 共享目录传输和每日招聘网站采集必须分开统计。定时任务使用
`docker compose run --pull never`，缺少已验证镜像时应预检失败，不得在每日采集中自动拉取、构建或安装
Chromium。

构建也有明确边界：两个运行 Compose 文件的 `collector` 服务只有 `image` 没有 `build`，所以运行和定时
采集不会隐式进入 Docker 构建流程。代码层更新使用 `deploy/build-collector-offline.sh`，它从本地
`job-market-monitor-collector:vm-base` 复制已安装依赖，强制 `--pull=false --network=none`，并使用
`pip --no-deps --no-build-isolation`。只有 Playwright/Chromium 版本升级才手工构建根目录 `Dockerfile`，
且必须显式传入 `--build-arg ALLOW_NETWORK_BUILD=1`。API/Web 代码层更新分别使用
`deploy/build-api-offline.sh` 和 `deploy/build-web-offline.sh`，从已验证的本地运行镜像断网构建；Web 先在
工作区完成生产构建，只覆盖产物并复用 Ubuntu 镜像中的 Linux 运行依赖。原始 API/Web Dockerfile 只用于
依赖版本变化时的一次性发布。后续 Agent 不得用普通 Compose 构建替代这些离线流程。

已验证的 Ubuntu 并发基线：并发 2 比顺序执行快约 37% 至 40%；并发 3 额外收益很小并增加 swap；并发
4 出现明显 swap 且更慢。因此不要仅按 CPU 核数提高并发，任何调整都必须重复完整性和内存实验。

近期来源优化要点：

- 字节跳动：每个渠道只初始化一次 SPA，后续分区复用页面状态，跨分区先用官网“清除”恢复根筛选；
  不得为节省导航请求而跳过分类/城市分区或分页。
- 蚂蚁集团：设置同源 `ctoken` 后，以轻量 `robots.txt` 页面内 `fetch` 官方分类和岗位 POST 接口，不加载
  完整 SPA；Ubuntu 两次完整轮次均为 1,166 个岗位且集合哈希一致，发生岗位版本冲突时会丢弃该轮并重抓。
- 任何来源的实时岗位数会变化。不同时间的完整轮次哈希不一致不一定是代码回归，必须结合声明总数、
  连续重复轮次和岗位差异判断。

## 数据模型与分析约束

数据库不是“当天岗位表”，而是历史观测模型：

- `jobs`：来源内稳定岗位身份
- `job_versions`：岗位正文、标题、要求等内容版本，内容未变化时复用
- `job_observations`：某次采集看到了哪个岗位版本
- `job_version_locations`、`job_version_location_cities`、`job_version_source_categories`：岗位版本的
  原始地点、城市级派生关联和官网分类事实
- `daily_snapshots`：来源/渠道当天最后一次完整权威快照
- `job_lifecycle_events`：首次出现、变更、缺失、关闭、恢复、重新开放
- `raw_snapshots`、字段统计和采集运行表：原始证据、覆盖率、问题和进度

`daily_snapshots` 还包含由上述事实表计算、可完整重建的每日计数读模型；`daily_snapshot_city_stats` 保存
每个权威快照的标准城市覆盖数和折算岗位数；`job_search_documents` 保存当前岗位三个正文搜索字段的字符
签名。总览、趋势、城市和非连续搜索应优先读取这些小型读模型，不要重新引入逐请求扫描全部观测、地点或
生命周期事件的查询。写入与同日快照替换必须在采集事务内同步覆盖汇总值，搜索仍需执行逐字顺序终检，
`check-data` 负责校验所有读模型与事实表一致。

每天重复采集不能把所有岗位正文重复插入数据库；通过岗位稳定身份、内容哈希版本和观测关系去重，同时
保留描述变化。来源地点原文不可覆盖，城市规范化只用于统一维度和展示；没有具体城市的“全国/海外/区域”
不能伪造成某个城市。官网直接分类是来源事实，不等同于项目自定义主题标签。

任何新增统计都必须明确口径：岗位数、岗位覆盖率、折算岗位数、公司数和招聘人数不是同一个指标；一个岗位
涉及多个城市时，城市覆盖率之和可能超过 100%。API 和前端必须显示快照日期及来源覆盖状态，不能把部分
覆盖包装成整个就业市场的完整结论。

## 开发、验证与提交

所有 shell 命令遵循仓库环境约定，使用 `rtk` 前缀；编辑文件使用 `apply_patch`。先查看工作区状态，
不要撤销或覆盖其他会话的未提交修改。

Python 后端常用验证：

```bash
rtk uv run pytest -q
rtk uv run ruff check .
rtk bash -n deploy/run-scheduled-crawls.sh deploy/run-concurrency-benchmark.sh
```

前端改动还需要遵守 `frontend/AGENTS.md`，并在 `frontend/` 下运行 TypeScript 检查和生产构建。
Compose 或部署脚本改动后至少检查：

```bash
rtk docker compose config --quiet
rtk git diff --check
```

真实采集只在 Ubuntu Docker 中验证，dry-run 不写 PostgreSQL 或原始数据；实际入库前先执行
`check-schema`、`check-data` 和 `check-runtime`。提交前确认 `git status` 中没有真实数据、`.env`、
Docker volume、实验输出或大文件。

提交应按一个可验证的主题组织，提交信息简洁明确。若修改了采集逻辑，提交说明或实验文档必须记录：
来源、渠道、岗位数、页数、完整性结果、`collection_hash`、耗时、Docker 收发量、内存/swap，以及
是否存在官网实时数据变化。不能只报告“更快”或“流量更低”。

## 给后续 Agent 的工作顺序

1. 阅读本文件、相关 `docs/` 和最近提交；执行 `rtk git status --short`，识别已有未提交改动。
2. 先在本机测试和 Ubuntu dry-run 复现问题，确认数据库、API、Web 和定时器的实际路径与状态。
3. 做最小范围修改，保留来源原始字段和完整性保护；不要用删字段、少分页或关闭校验换指标。
4. 运行后端/前端/Compose 验证，再进行 Ubuntu 实验；候选镜像先用独立 tag，验证后再备份旧 `latest`
   并提升标签。
5. 部署时只更新采集器代码/镜像，除非用户明确要求，不要重启 API、Web 或 PostgreSQL。
6. 在最终回复中说明实际改动、实验数据、验证结果、Ubuntu 当前状态和未完成风险；不要把测试数据说成
   真实全量数据，也不要暗示已经部署生产。
