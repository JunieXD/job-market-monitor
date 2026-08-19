# Ubuntu 连续试运行

## 目标

让 Ubuntu Parallels 虚拟机连续运行网站、API、PostgreSQL 和每日真实采集，至少观察三个自然日，
确认招聘站访问稳定性、日快照语义、失败隔离、磁盘增长和趋势页面都符合预期。

## 运行约束

- 默认每天上海时间 03:15 触发，最多随机延迟 5 分钟。
- 每个来源渠道每天最多保留一个 `daily_snapshots` 标准记录。
- 正常 timer 只采集当天缺少标准快照的来源；已完成来源自动跳过。
- 同日人工补跑的最后一次完整权威结果会替换当天日快照指针，全部 `crawl_runs` 和原始证据仍保留。
- 同日补跑不会再次增加岗位缺失次数，避免一天被计算为多个生命周期周期。
- 单个来源失败不会中止后续来源；采集状态页面必须显示真实失败或不完整结果。
- 真实岗位数据、数据库和原始响应只保存在 Ubuntu Docker volume，不进入 Git。

## 常驻服务

```bash
cd /opt/job-market-monitor
docker compose up -d postgres api web
docker compose ps
```

期望 `postgres` 为 healthy，`api` 和 `web` 为 running。API 与网站配置了
`restart: unless-stopped`，虚拟机或 Docker 服务重启后会自动恢复。

## 每日调度

虚拟机试运行使用包含本地 PostgreSQL 的 `compose.yaml`。安装 unit 后同时安装虚拟机 override，避免误用只连接外部数据库的生产 Compose：

```bash
sudo install -m 0644 deploy/systemd/job-market-crawl.service \
  /etc/systemd/system/job-market-crawl.service
sudo install -m 0644 deploy/systemd/job-market-crawl.timer \
  /etc/systemd/system/job-market-crawl.timer
sudo install -D -m 0644 deploy/systemd/job-market-crawl.vm.conf \
  /etc/systemd/system/job-market-crawl.service.d/compose.conf
sudo systemctl daemon-reload
sudo systemctl enable --now job-market-crawl.timer
```

```bash
systemctl list-timers job-market-crawl.timer
systemctl status job-market-crawl.timer --no-pager
journalctl -u job-market-crawl.service -n 200 --no-pager
```

需要在试运行首日立即开始时：

```bash
sudo systemctl start job-market-crawl.service
```

调度脚本使用 `flock` 防止两个批次并发；每个来源有独立超时、重试、清理和失败恢复。

## 观察入口

- 网站：`http://<Ubuntu虚拟机IP>:3000`
- 采集进度：`http://<Ubuntu虚拟机IP>:3000/collection`
- API 文档：`http://<Ubuntu虚拟机IP>:8000/docs`
- 进度 API：`http://<Ubuntu虚拟机IP>:8000/api/v1/collection/status`

采集进度页面每 15 秒刷新，显示完成、运行、失败、结果不完整、等待采集和每个来源的岗位/页面数量。

## 每日检查

```bash
cd /opt/job-market-monitor
docker compose run --rm collector check-schema
docker compose run --rm collector check-data
docker compose run --rm collector check-runtime
curl -fsS http://127.0.0.1:8000/healthz
```

`check-source-health` 在所有来源都尚未稳定前可能返回非零，这是需要观察的结果，不应隐藏。使用
采集状态页面和 systemd 日志定位具体来源，不能通过登录态、验证码绕过或代理池规避招聘站限制。

## 三日验收标准

1. 网站和 API 连续可访问，容器没有意外退出或重启循环。
2. timer 每个自然日触发一次；重复启动时已完成来源被跳过。
3. 至少三个日期出现在趋势页，且每个日期的覆盖率和完成来源清晰可见。
4. 成功来源每天只有一个标准快照；同日补跑后页面读取最后一次权威结果。
5. 单个来源失败后后续来源仍有采集运行记录，错误摘要能在采集状态页查看。
6. schema、数据完整性和运行时磁盘检查每天通过。
7. 岗位、分类和城市页面的日期、覆盖率与总览一致，不出现内部渠道代码。
