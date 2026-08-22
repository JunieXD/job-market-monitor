# LLM 岗位画像提取

岗位画像提取是独立于采集器的影子处理层。它只读取已经进入权威每日快照的岗位版本，不影响采集成功、
快照生成和岗位关闭判断。默认关闭，也不会进入现有 API 或页面。

## V1 提取范围

`job-profile-v1` 提取岗位族、细分方向、业务/技术领域、资历、经验、学历与专业、技能、语言、特殊工作
条件、招聘面向对象，以及最多三条职责和任职要求摘要。程序把数据库中的完整 `job_version.payload` 原样放入 `source_job`
交给模型，包括正文、官网分类、部门、业务单元、地点和官网直接提供的学历/经验等字段；不预选语义字段，
也不按标点、换行或编号预先切分，字段内部结构由模型自行理解。模型可以返回来源字段和原文 quote 作为解释
上下文，但程序不做逐字匹配、字符位置还原或技能名校验，不会因为证据格式差异拒绝保存画像。
未明确出现的字段保持 `NULL` 或空数组。

岗位族、方向和领域使用受控 taxonomy，以便跨公司聚合；明确存在但 taxonomy 尚未覆盖的类别使用 `other`
并保留模型提取的 `other_name`，不会丢成一个无名称的杂项。技能名称不是预设词表，模型可以提取任意原文明示
的具体硬技能；taxonomy 只约束技能的粗分类和其他稳定统计维度。

预设以中文提供给模型：`taxonomy.json` 的 `labels_zh` 是分类名称，分类映射中的中文文本是定义，`examples`
是典型示例，`boundaries` 是容易混淆类别的正反边界；资历、学历、要求强度和工作条件的中文对照位于
`field_values_zh`。JSON 输出仍使用英文稳定 key（例如 `software_engineering` 对应“软件工程”），因为这些
key 已进入 Schema、数据库画像和跨岗位聚合；模型不需要猜英文含义，只需按中文预设理解后返回对应 key。

Prompt、严格 JSON Schema 和 taxonomy 位于：

```text
src/job_market/derivation_profiles/job-profile-v1/
```

任一文件、模型、端点、推理档位或输出上限变化都会改变 `derivation_profile_id`。数据库保存完整配置及
Prompt、Schema、taxonomy 的 SHA-256，但不会保存 API key。

### 缓存友好的请求结构

每次请求的消息固定为三个部分：第一条 `system` 是不随岗位变化的通用协议，第二条 `system` 是按 profile
版本固定的完整中文 taxonomy，最后一条 `user` 才是完整且唯一变化的 `source_job`。岗位 payload 使用稳定的
UTF-8 JSON 序列化和排序后的 key；不会把岗位标题、岗位 ID、时间戳或随机 request ID 插入固定前缀。这样在
同一个 profile 连续调用时，服务商若支持自动 prompt-prefix caching，可以复用尽可能长的前缀；切换 prompt、
Schema、taxonomy、模型或端点会自然产生新的 profile 前缀。StepFun 是否实际计费缓存命中由接口返回的 usage
字段决定，客户端兼容读取 `prompt_tokens_details.cached_tokens`、`cached_tokens` 和
`prompt_cache_hit_tokens`，没有字段时不假设命中。

`recruitment_audience` 将官网的招聘阶段和届次归一化为可筛选字段：`new_graduate`、`internship`、
`experienced`、`trainee`、`talent_program`、`flexible_employment`、`general` 或 `other`，并保留
`graduation_years`（如 `[2027]`）。没有明确年份时年份数组为空，不从发布时间、公司名称或岗位类别猜测。

### 调用审计与日志

每个实际请求先在 `llm_call_logs` 写入 `running` 行，完成后更新为 `succeeded` 或 `failed`；一次重试会新建
一行，不会覆盖上一次调用。表中保存 `started_at`/`finished_at`、provider、model、endpoint、推理档位、
attempt、input/request hash、provider request ID、finish reason、输入/缓存/输出/总 token、错误和解析后的
output JSON。标准 JSON 日志同时发出 `llm_call_started` 和 `llm_call_finished` 事件，包含同样的定位字段和
token 元数据，但不打印岗位正文、完整 prompt、API key 或完整 JSON 输出，避免把真实招聘数据写入 journald
或 Docker 日志；完整输入和输出仍可通过数据库关联查询：

```sql
SELECT c.*, v.payload AS input_source_job
FROM llm_call_logs AS c
JOIN job_versions AS v ON v.id = c.job_version_id
WHERE c.derivation_run_id = '<run-id>'
ORDER BY c.started_at, c.id;
```

`job_version_derivations` 保留该岗位版本最后一次尝试的汇总状态，`llm_call_logs` 才是逐调用（含失败和重试）
的审计明细。`derive-jobs` 的 `call_count`、失败调用数和 token 汇总也从这张表计算，因此重试不会被任务行
覆盖。缓存字段来自 StepFun usage；接口未返回缓存字段时保持 `NULL`，不把未知当作未命中。

## StepFun 配置

在部署机未跟踪的 `.env` 中配置：

```dotenv
LLM_ENABLED=true
STEPFUN_API_KEY=<本地密钥>
STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1/chat/completions
STEPFUN_MODEL=step-3.5-flash
LLM_REASONING_EFFORT=low
LLM_CONCURRENCY=5
LLM_REQUEST_TIMEOUT_SECONDS=120
LLM_MAX_TOKENS=32768
LLM_MAX_ATTEMPTS=3
LLM_STALE_AFTER_MINUTES=30
```

`.env` 权限应为 `600`，不得提交 Git。密钥只传入 Compose 的 `deriver` 服务，不传给 API、Web 或日常
采集容器。

这里必须使用 Step Plan 专用端点，不能替换成普通余额端点 `https://api.stepfun.com/v1/chat/completions`。
StepFun 当前文档把 `step-3.5-flash` 列为 Step Plan 支持模型；2026-08-21 已用上述专用端点完成最小请求和
项目严格 JSON Schema 请求验证。V1 使用 `reasoning_effort=low`。真实官网岗位对照中，4096 和 8192
上限均出现 `finish_reason=length`；此前的严格结构化测试中，同一数仓岗位在 16384 上限下已经使用 10228
completion tokens。现在将上限提高到模型接口允许的 32768，避免复杂岗位因输出上限导致失败。上限不是预扣用量，
仍按实际生成量计费；只有接口返回非完整响应或 JSON 无法解析时才算失败。

`prompt.md` 只定义通用提取协议：完整理解来源、只根据官网内容提取、按核心职责分类、只提取硬技能、
归一化要求强度、返回解释上下文和严格遵守 Schema。具体字段规则、中文分类定义、正反例与易混淆边界
全部位于 `taxonomy.json`；不再把某个岗位或某组分类的补丁不断追加到主 Prompt。

2026-08-22 使用 Step Plan 专用端点、`step-3.5-flash`、`reasoning_effort=low` 和 32768 输出上限，针对本机
Ubuntu 数据库的真实权威在招岗位完成四轮共 16 次调用，未写回数据库。样本覆盖大模型训练系统、数据仓库、
AI Agent 产品、游戏战斗策划、信贷反欺诈、海外 AI 解决方案架构、用户研究和 AI SoC 芯片设计。16 次均以
`finish_reason=stop` 完成；单次 prompt 为 6,228 至 8,405 tokens，completion 为 3,674 至 9,484 tokens，
总计 11,527 至 17,372 tokens。全部调用累计 240,834 tokens，其中 prompt 119,253、completion 121,581。

真实迭代修正了以下问题：无数字年限不再生成上下限同时为空的经验对象；经验年限和“分析师”等职业名称
不再推断资历；数仓岗位不再因宽泛的“AI & BI”官网分类进入 `ai_platform`；AI Agent 产品岗位不再附加
`nlp_llm`；反欺诈策略技能归入安全技术；大模型训练系统不再进入 `devops_sre`、`data_engineering` 或仅凭
公司名称进入游戏领域。最终复测中，大模型训练系统正确得到 `software_engineering`、`machine_learning`、
`nlp_llm`、`ai_platform` 和 `intern`，但仍偶发额外输出 `backend`；部分硬件描述语言和大数据组件的技能粗
分类也有波动。当前结果适合继续作为影子画像观察，不能替代固定标注评估集，也不能直接接入用户筛选。

### 分层随机复测（2026-08-22）

在同一数据库的当前在招且正文版本最新集合中，使用固定种子
`layered-random-20260822-v2` 按岗位标题分为算法/数据、软件工程、产品/设计/研究、运营/市场/商务、
内容/游戏、硬件/制造、财务/风控/客服和企业职能八层，并尽量保证来源公司不重复。实际抽取 16 个岗位，
覆盖阿里、字节、腾讯、京东、小米、华为、快手、哔哩哔哩、蚂蚁、同程、网易、米哈游和阿里云等不同来源。
所有请求均使用 Step Plan `step-3.5-flash`、`reasoning_effort=low`、并发 5、32768 上限，16/16 返回
`finish_reason=stop`，没有因输出上限失败；prompt 约 9,500--10,800 tokens，completion 约
3,670--12,335 tokens。

这轮真实岗位中，软件验证、供应链规划、财务分析、风险策略、游戏服务端、招聘专家、国际化商务和大模型
算法等画像大体可用。复测推动了以下通用边界更新：数字经验不能推断 mid/senior；在读、秋招和招聘渠道
本身不能单独推断资历；无数字年限的经验字段返回 null；招聘职能不自动成为人力资源服务领域；测试 AI
或云产品不等于自然语言模型研发；软件测试/验证的岗位族优先为软件工程。

定向复测中，以上边界大部分已生效（无数字经验空对象、`human_resources_services` 和测试岗位的
`nlp_llm` 误判均消失），但同一社会招聘岗位在多次独立调用中仍偶发把 1 年或 3--5 年经验推成 `mid`，
且供应链岗位的岗位族在运营与供应链之间有随机波动。这是模型遵循性和采样稳定性风险，当前不增加程序语义
后处理；随后补充“采购/供应链核心职责优先供应链与采购”并复测，岗位族稳定回到
`supply_chain_procurement`，但 seniority 仍可能受数字经验干扰。画像仍保持影子状态，后续评估需要固定
人工标注集并统计重复调用一致率。

### 缓存与招聘面向对象实测（2026-08-22）

当前数据库只读审计得到 47,338 个在招岗位：`campus` 11,849 个、`experienced` 29,020 个、`internship`
2,854 个、`general` 3,615 个；1,075 个岗位已有结构化毕业时间范围。招聘项目中真实出现
`2027届校园招聘`、`2027届秋招`、`2026应届生项目`、`ByteIntern`、`青云计划-实习生`、
`Seed大模型人才实习招聘`、`顶尖人才`、`岗位外包`和`派遣`等表达。正文或项目中的届次以 `2027届` 为主，
同时存在 2026、2028 以及少量更早届次。

使用固定种子 `layered-random-20260822-cache-audience-v1`，按招聘阶段（应届、实习、社招、灵活用工、
普通招聘）和岗位类型分层，抽取 16 个岗位、覆盖 13 个来源。新增字段 16/16 成功返回：典型结果包括
`2027届校园招聘 -> new_graduate + [2027]`、`ByteIntern -> internship`、社会招聘 -> `experienced`、
灵活用工岗位 -> `experienced + flexible_employment`、校招储备实习 -> `internship + new_graduate`。
其中 2027 届、实习、社招和灵活用工的分类均能从岗位的 channel、employment_type、recruitment_project 或
正文得到支持；没有届次时 `graduation_years` 保持空数组。

本批 prompt token 约 11,029--11,522，completion 约 3,163--11,500，全部 `finish_reason=stop`。16 次并发
调用中有 11 次 usage 返回 `cached_prompt_tokens=10,560`，其余 5 次没有返回缓存字段；taxonomy 规则调整后
对两个岗位的复测均返回 `cached_prompt_tokens=6,464`。这证明固定 system 协议 + 固定 taxonomy + 最后动态
`source_job` 的消息结构可以获得真实前缀复用，但未返回字段不能当作未命中，最终计费仍以 StepFun usage 为准。

### 真实数据覆盖审计

2026-08-21 对本机 Ubuntu 当前权威在招版本做了只读审计：共 47,392 个岗位版本，来自 32 个来源；其中
43,797 个岗位（92.4%）带官网分类，共涉及 714 个不同的来源分类。完整 `job_version.payload` 序列化后的
字符长度中位数为 1,694，P90 为 2,207，P99 为 3,687，最长 14,312；仅 10 个岗位超过 8,000 字符，
没有岗位超过 16,000 字符。因此当前数据不需要通过切段控制模型输入长度。

原 V1 taxonomy 对通用岗位族覆盖尚可，但细分方向明显偏技术岗位。用官网分类和岗位标题做关键词下限审计时，
标题中出现解决方案/架构/咨询 2,065 个、内容/编辑/编导 1,652 个、风险/风控/反欺诈 918 个、战略/投资
454 个、游戏策划/关卡/数值 304 个、审核/内容安全 298 个、用户研究 212 个。这些类别此前只能被塞入相近
但含义不同的方向或 `other`。V1 已据此补充这些高频方向，以及门店、教育、政府事务、地产、工业、能源、
电信等明显缺口；这仍是受控分类的第一版，不代表已经通过准确率评估。

## 影子运行

先升级数据库，再只查看候选版本：

```bash
docker compose run --pull never --rm collector init-db
docker compose run --pull never --rm deriver derive-jobs --dry-run --limit 20
```

`--dry-run` 不调用模型，也不创建 profile 或任务记录。第一次实际验证只处理少量岗位：

```bash
docker compose run --pull never --rm deriver derive-jobs --limit 5
```

可以加 `--source bytedance` 或 `--channel experienced` 缩小范围。命令输出本批成功/失败数、token 用量和
累计状态，不输出岗位正文或 API key。成功任务由数据库唯一约束保证不会在后续批次中再次调用；失败任务
最多按 `LLM_MAX_ATTEMPTS` 重试。

## 自动增量运行

代表样本完成离线评估和人工复核后，才安装独立 timer：

```bash
sudo install -m 0755 deploy/run-scheduled-derivations.sh \
  /opt/job-market-monitor/deploy/run-scheduled-derivations.sh
sudo install -m 0644 deploy/systemd/job-market-derive.service \
  /etc/systemd/system/job-market-derive.service
sudo install -m 0644 deploy/systemd/job-market-derive.timer \
  /etc/systemd/system/job-market-derive.timer
sudo install -D -m 0644 deploy/systemd/job-market-derive.vm.conf \
  /etc/systemd/system/job-market-derive.service.d/compose.conf
sudo systemctl daemon-reload
sudo systemctl enable --now job-market-derive.timer
```

timer 默认每天上海时间 06:00 运行，晚于 03:15 的采集窗口；单批最多处理 100 个版本，并发由
`LLM_CONCURRENCY` 控制。首次历史回填应保持小批量，检查准确率和 token 成本后再通过 systemd drop-in
调整 `DERIVATION_BATCH_LIMIT`。定时脚本强制 `--pull never`，不构建镜像、不安装依赖。

## 发布边界

`job-profile-v1` 当前只写影子结果，`derivation_profiles.is_current` 保持 false。将结果接入岗位筛选、详情
页或趋势分析前，必须先固定评估集和指标，检查各字段精确率、证据命中率、空值策略、失败分布和模型成本，
再通过单独迁移或发布命令显式切换 profile。不能直接把影子结果当成官网事实展示。
