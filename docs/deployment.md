# 部署说明

本文说明如何在本地 Ubuntu 虚拟机中验证 Docker 运行方式，以及将采集器接入一台已经运行
PostgreSQL 的服务器。当前项目尚未自动连接或修改任何生产服务器。

## 本地 Ubuntu 验证

建议把项目放在 Ubuntu 虚拟机自己的可写目录中，不要直接在 Parallels 只读共享目录中构建镜像。

```bash
git clone <你的 GitHub 仓库地址> /opt/job-market-monitor
cd /opt/job-market-monitor
cp .env.example .env
docker compose build collector
docker compose up -d postgres
docker compose run --rm collector init-db
docker compose up -d api web
```

先用小范围 dry-run 检查页面和解析器：

```bash
docker compose run --rm collector crawl \
  --source bytedance \
  --channel campus \
  --dry-run \
  --max-pages 1
```

dry-run 不写入 PostgreSQL 或原始数据。确认输出中的 `complete`、岗位数量和分区信息后，再执行
单来源实际采集：

```bash
docker compose run --rm collector crawl \
  --source bytedance \
  --channel campus
```

## 生产服务器配置

生产配置使用 [compose.production.yaml](../compose.production.yaml)。它只有 `collector` 服务，
通过外部 Docker network 访问服务器上已有的 PostgreSQL，不会创建或接管 PostgreSQL 容器。

1. 将代码放到服务器上的固定目录，例如 `/opt/job-market-monitor`。
2. 确认 PostgreSQL 容器所在的 Docker network：

   ```bash
   docker network ls
   docker inspect <postgresql容器名> --format '{{json .NetworkSettings.Networks}}'
   ```

3. 复制 `.env.production.example` 为服务器本地的 `.env.production`，填写真实的
   `DATABASE_URL` 和 `DATABASE_DOCKER_NETWORK`。该文件不得提交到 Git。
4. 在目标数据库中执行初始化或迁移，并检查 schema：

   ```bash
   docker compose --env-file .env.production \
     -f compose.production.yaml run --rm collector init-db
   docker compose --env-file .env.production \
     -f compose.production.yaml run --rm collector check-schema
   docker compose --env-file .env.production \
     -f compose.production.yaml run --rm collector check-data
   ```

   已有数据库若包含旧版本数据，应先备份，再让 Alembic 按迁移顺序升级；不要手工删除表。

5. 先运行一个来源的 dry-run，再运行实际入库。确认正常后才安装 systemd timer。

`api` 和 `web` 使用 `restart: unless-stopped`，Docker 服务或虚拟机重启后会自动恢复。可通过
`docker compose ps` 检查三个常驻服务；采集器只在定时任务执行时创建临时容器。

定时脚本会在启动前检查采集镜像，并使用 Docker Compose 的 `--pull never` 运行采集容器。镜像和
Chromium 依赖必须在发布阶段手工构建并验证；每日采集不会自动触发镜像拉取、构建或重复下载依赖。

## systemd 定时任务

批量脚本会读取当天尚未生成标准快照的来源，并为每个来源独立创建临时采集容器。单个来源失败会
被记录并继续运行后续来源；批次末尾会执行统一检查。来源部分成功或失败会让批次以降级状态结束，
但 systemd 仍视为已正常完成调度；数据库结构、磁盘或数据一致性检查失败才会让 service 失败。
当天已经完成全部渠道的来源自动跳过，因此 timer 重启或重复触发不会重复采集完整来源。

默认最多并发 2 个来源，来源启动间隔 3 秒。并发只跨来源发生，同一来源的多个渠道仍按顺序执行；
单来源失败或超时不会取消其他来源。Ubuntu 资源较紧时先设为 `MAX_PARALLEL_SOURCES=1`，完成
矩阵实验后再提升；不要把并发数直接等同于 CPU 核数。

```bash
sudo install -m 0644 deploy/systemd/job-market-crawl.service \
  /etc/systemd/system/job-market-crawl.service
sudo install -m 0644 deploy/systemd/job-market-crawl.timer \
  /etc/systemd/system/job-market-crawl.timer
sudo install -m 0644 deploy/logrotate/job-market-monitor \
  /etc/logrotate.d/job-market-monitor
sudo systemctl daemon-reload
sudo systemctl enable --now job-market-crawl.timer
```

查看下次运行时间和最近日志：

```bash
systemctl list-timers job-market-crawl.timer
journalctl -u job-market-crawl.service -n 200 --no-pager
tail -n 100 /var/log/job-market-monitor/crawl.jsonl
```

采集日志为一行一个 JSON 事件，记录批次、来源、渠道、重试、每分钟进度、结果、耗时和流量汇总。

定时任务启动前会检查采集镜像是否已存在，并使用 `docker compose run --pull never`。镜像和
Chromium 依赖必须在发布阶段构建，定时任务不会触发镜像拉取、构建或依赖下载。
完整 traceback 与最多 100 条结构化问题明细保存在 `crawl_runs`，日志不写岗位正文和响应 payload。
专用日志单文件上限 20MB、保留 14 天并压缩；Docker `json-file` 日志另有每容器 30MB 上限。

需要立即开始一次真实采集时，可以手工启动同一个 oneshot 服务；该操作仍受文件锁和当日完成状态保护：

```bash
sudo systemctl start job-market-crawl.service
systemctl status job-market-crawl.service --no-pager
```

默认每天上海时间 03:15 运行，带有持久化触发和最多 5 分钟的随机延迟。批次总超时为 23 小时，
单来源默认超时为 3 小时；可以在 service 的环境变量中按服务器资源调整。

采集器的 `CRAWL_BLOCK_NONESSENTIAL_RESOURCES=true` 和 `CRAWL_BLOCK_SERVICE_WORKERS=true` 默认开启。
前者通过 CDP URL 模式阻止图片、字体、音视频而保留 HTTP 缓存；后者阻止后台 Service Worker。
遇到某个站点页面依赖被阻止资源时，应只对该来源做小范围对照测试后再决定是否关闭，不要全局关闭。

## 运行前检查

- 服务器可以访问招聘官网，且不需要图形界面或登录态。
- PostgreSQL network 名称和 `DATABASE_URL` 与实际容器一致。
- Docker named volume 所在磁盘保留至少 `RAW_MIN_FREE_GIB` 指定的空间。
- 服务器时间和时区配置正确，便于计算每日快照日期。
- `.env.production`、数据库密码、原始岗位数据和备份文件没有加入 Git。

出现单个来源错误时，先查看该来源的 service 输出和数据库中的失败运行记录；不要为了绕过限制而
加入个人 Cookie、验证码绕过、代理池或指纹伪装。

## 网站服务

本地 Compose 的网站入口为 `http://<Ubuntu虚拟机IP>:3000`，API 入口为
`http://<Ubuntu虚拟机IP>:8000/docs`。前端容器通过 `API_INTERNAL_URL=http://api:8000` 访问 API，
浏览器只访问网站同源的 `/api/v1` 路径。

`http://<Ubuntu虚拟机IP>:3000/collection` 会每 15 秒更新当天的完成、运行、失败、结果不完整和
等待采集渠道。该页面的数据直接来自 `crawl_runs` 和 `daily_snapshots`，不是读取 systemd 日志猜测。

生产 Compose 不映射宿主端口，网站和 API 会加入现有 PostgreSQL network。需要将服务器已有的
Nginx/Caddy 反向代理到 `web:3000`；不要把 API 端口直接暴露到公网。
