import argparse
import asyncio
import json
import sys
import traceback
from collections import Counter
from datetime import timedelta
from typing import Any

from playwright.async_api import async_playwright

from job_market.browser_network import BrowserNetworkMetrics
from job_market.config import Settings
from job_market.connectors.alibaba import AlibabaConnector
from job_market.connectors.alibaba_international import AlibabaInternationalConnector
from job_market.connectors.alibaba_taotian import AlibabaTaoTianConnector
from job_market.connectors.aliyun import AliyunConnector
from job_market.connectors.ant import AntConnector
from job_market.connectors.baidu import BaiduConnector
from job_market.connectors.beike import BeikeConnector
from job_market.connectors.bilibili import BilibiliConnector
from job_market.connectors.bytedance import ByteDanceConnector
from job_market.connectors.cainiao import CainiaoConnector
from job_market.connectors.ctrip import CtripConnector
from job_market.connectors.didi import DidiConnector
from job_market.connectors.huawei import HuaweiConnector
from job_market.connectors.iqiyi import IqiyiConnector
from job_market.connectors.jd import JDConnector
from job_market.connectors.kuaishou import KuaishouConnector
from job_market.connectors.kuaishou_campus import KuaishouCampusConnector
from job_market.connectors.lenovo import LenovoConnector
from job_market.connectors.meituan import MeituanConnector
from job_market.connectors.netease import NetEaseConnector
from job_market.connectors.oppo import OppoConnector
from job_market.connectors.pdd import PDDConnector
from job_market.connectors.qihu360 import Qihu360Connector
from job_market.connectors.tencent import TencentConnector
from job_market.connectors.tencent_social import TencentSocialConnector
from job_market.connectors.tongcheng import TongchengConnector
from job_market.connectors.vivo import VivoConnector
from job_market.connectors.xiaohongshu import XiaohongshuConnector
from job_market.connectors.xiaomi import XiaomiConnector
from job_market.db import check_schema, create_schema, make_engine
from job_market.health import SourceHealthChecker
from job_market.quality import DataQualityChecker
from job_market.raw_store import RawStore
from job_market.repository import Repository
from job_market.runtime_checks import RuntimeChecker
from job_market.schemas import Channel, CollectionResult

SOURCE_SPECS = {
    "bytedance": {
        "key": "bytedance_cn",
        "company_key": "bytedance",
        "company_name": "字节跳动",
        "display_name": "字节跳动中国招聘官网",
        "source_type": "company_career_portal",
        "scope_name": "字节跳动",
        "base_url": "https://jobs.bytedance.com",
        "connector": ByteDanceConnector,
        "channels": {
            Channel.CAMPUS: "字节跳动中国招聘官网校园招聘公开岗位",
            Channel.EXPERIENCED: "字节跳动中国招聘官网社会招聘公开岗位",
        },
    },
    "alibaba": {
        "key": "alibaba_cn",
        "company_key": "alibaba",
        "company_name": "阿里巴巴集团",
        "display_name": "阿里巴巴集团统一校园招聘",
        "source_type": "group_campus_portal",
        "scope_name": "阿里巴巴集团校园招聘",
        "base_url": "https://campus-talent.alibaba.com",
        "connector": AlibabaConnector,
        "channels": {
            Channel.CAMPUS: "当前公开的应届生、日常实习和研究型实习批次",
        },
    },
    "alibaba-taotian": {
        "key": "alibaba_taotian_social",
        "company_key": "alibaba",
        "company_name": "阿里巴巴集团",
        "display_name": "淘天集团社会招聘",
        "source_type": "business_unit_career_portal",
        "scope_name": "淘天集团",
        "base_url": "https://talent.taotian.com",
        "connector": AlibabaTaoTianConnector,
        "channels": {
            Channel.EXPERIENCED: "淘天集团社会招聘站公开岗位",
        },
    },
    "tencent": {
        "key": "tencent_cn",
        "company_key": "tencent",
        "company_name": "腾讯",
        "display_name": "腾讯校园招聘",
        "source_type": "company_campus_portal",
        "scope_name": "腾讯校园招聘公开岗位",
        "base_url": "https://join.qq.com",
        "connector": TencentConnector,
        "channels": {Channel.CAMPUS: "应届生、实习生及人才专项公开岗位"},
    },
    "tencent-social": {
        "key": "tencent_social_cn",
        "company_key": "tencent",
        "company_name": "腾讯",
        "display_name": "腾讯社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "腾讯中国社会招聘公开岗位",
        "base_url": "https://careers.tencent.com",
        "connector": TencentSocialConnector,
        "channels": {Channel.EXPERIENCED: "官网社招筛选下的中国公开岗位"},
    },
    "meituan": {
        "key": "meituan_cn",
        "company_key": "meituan",
        "company_name": "美团",
        "display_name": "美团招聘",
        "source_type": "company_career_portal",
        "scope_name": "美团公开社招与校招岗位",
        "base_url": "https://zhaopin.meituan.com",
        "connector": MeituanConnector,
        "channels": {
            Channel.EXPERIENCED: "美团社会招聘公开岗位",
            Channel.CAMPUS: "美团校园招聘公开岗位",
        },
    },
    "jd": {
        "key": "jd_cn",
        "company_key": "jd",
        "company_name": "京东集团",
        "display_name": "京东招聘",
        "source_type": "company_career_portal",
        "scope_name": "京东社会招聘公开岗位",
        "base_url": "https://zhaopin.jd.com",
        "connector": JDConnector,
        "channels": {Channel.EXPERIENCED: "京东社会招聘公开岗位"},
    },
    "netease": {
        "key": "netease_cn",
        "company_key": "netease",
        "company_name": "网易",
        "display_name": "网易社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "网易公开职位页（混合全职、实习与派遣）",
        "base_url": "https://hr.163.com",
        "connector": NetEaseConnector,
        "channels": {Channel.GENERAL: "官网混合职位页，工作性质由官网字段区分"},
    },
    "xiaomi": {
        "key": "xiaomi_cn",
        "company_key": "xiaomi",
        "company_name": "小米",
        "display_name": "小米招聘",
        "source_type": "company_career_portal",
        "scope_name": "小米公开招聘项目",
        "base_url": "https://hr.xiaomi.com",
        "connector": XiaomiConnector,
        "channels": {
            Channel.EXPERIENCED: "小米社招公开岗位",
            Channel.CAMPUS: "小米校招公开岗位（含顶尖人才子项目成员关系）",
            Channel.INTERNSHIP: "小米实习公开岗位",
        },
    },
    "huawei": {
        "key": "huawei_cn",
        "company_key": "huawei",
        "company_name": "华为",
        "display_name": "华为招聘",
        "source_type": "company_career_portal",
        "scope_name": "华为公开社会招聘与校园招聘岗位",
        "base_url": "https://career.huawei.com",
        "connector": HuaweiConnector,
        "channels": {
            Channel.EXPERIENCED: "华为社会招聘公开岗位",
            Channel.CAMPUS: "华为校园招聘公开岗位（含应届生与实习生）",
        },
    },
    "kuaishou": {
        "key": "kuaishou_cn",
        "company_key": "kuaishou",
        "company_name": "快手",
        "display_name": "快手招聘",
        "source_type": "company_career_portal",
        "scope_name": "快手公开社会招聘与日常实习岗位",
        "base_url": "https://zhaopin.kuaishou.cn",
        "connector": KuaishouConnector,
        "channels": {
            Channel.EXPERIENCED: "快手社会招聘公开岗位（国内/国外分区）",
            Channel.INTERNSHIP: "快手日常实习公开岗位（国内/国外分区）",
        },
    },
    "kuaishou-campus": {
        "key": "kuaishou_campus_cn",
        "company_key": "kuaishou",
        "company_name": "快手",
        "display_name": "快手校园招聘",
        "source_type": "company_campus_portal",
        "scope_name": "快手当前应届生与留用实习招聘项目",
        "base_url": "https://campus.kuaishou.cn",
        "connector": KuaishouCampusConnector,
        "channels": {
            Channel.CAMPUS: "官网当前应届生项目公开岗位",
            Channel.INTERNSHIP: "官网当前留用实习项目公开岗位",
        },
    },
    "baidu": {
        "key": "baidu_cn",
        "company_key": "baidu",
        "company_name": "百度",
        "display_name": "百度校园与实习招聘",
        "source_type": "company_campus_portal",
        "scope_name": "百度公开校园招聘与实习生招聘岗位",
        "base_url": "https://talent.baidu.com",
        "connector": BaiduConnector,
        "channels": {
            Channel.CAMPUS: "百度校园招聘公开岗位",
            Channel.INTERNSHIP: "百度实习生招聘公开岗位",
        },
    },
    "didi": {
        "key": "didi_social_cn",
        "company_key": "didi",
        "company_name": "滴滴",
        "display_name": "滴滴社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "滴滴公开社会招聘岗位",
        "base_url": "https://talent.didiglobal.com",
        "connector": DidiConnector,
        "channels": {
            Channel.EXPERIENCED: "滴滴社会招聘官网公开岗位",
        },
    },
    "bilibili": {
        "key": "bilibili_cn",
        "company_key": "bilibili",
        "company_name": "哔哩哔哩",
        "display_name": "哔哩哔哩招聘",
        "source_type": "company_career_portal",
        "scope_name": "哔哩哔哩公开社招、应届生和实习岗位",
        "base_url": "https://jobs.bilibili.com",
        "connector": BilibiliConnector,
        "channels": {
            Channel.EXPERIENCED: "哔哩哔哩社会招聘公开岗位",
            Channel.CAMPUS: "哔哩哔哩应届生招聘公开岗位",
            Channel.INTERNSHIP: "哔哩哔哩实习生招聘公开岗位",
        },
    },
    "pdd": {
        "key": "pdd_cn",
        "company_key": "pdd",
        "company_name": "拼多多集团",
        "display_name": "拼多多集团校园招聘",
        "source_type": "company_campus_portal",
        "scope_name": "拼多多集团公开应届生和实习生岗位",
        "base_url": "https://careers.pddglobalhr.com",
        "connector": PDDConnector,
        "channels": {
            Channel.CAMPUS: "拼多多集团应届生招聘公开岗位",
            Channel.INTERNSHIP: "拼多多集团实习生招聘公开岗位",
        },
    },
    "ant": {
        "key": "ant_cn",
        "company_key": "ant",
        "company_name": "蚂蚁集团",
        "display_name": "蚂蚁集团社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "蚂蚁集团公开社会招聘岗位",
        "base_url": "https://talent.antgroup.com",
        "connector": AntConnector,
        "channels": {
            Channel.EXPERIENCED: "蚂蚁集团社会招聘公开岗位",
        },
    },
    "ctrip": {
        "key": "ctrip_cn",
        "company_key": "ctrip",
        "company_name": "携程集团",
        "display_name": "携程集团招聘",
        "source_type": "company_career_portal",
        "scope_name": "携程社会招聘页混合公开岗位",
        "base_url": "https://job.ctrip.com",
        "connector": CtripConnector,
        "channels": {
            Channel.GENERAL: "携程社会招聘页公开的混合全职、实习和其他岗位",
        },
    },
    "360": {
        "key": "qihu360_cn",
        "company_key": "qihu360",
        "company_name": "360集团",
        "display_name": "360社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "360公开社会招聘岗位",
        "base_url": "https://hr.360.cn",
        "connector": Qihu360Connector,
        "channels": {
            Channel.EXPERIENCED: "360社会招聘公开岗位",
        },
    },
    "oppo": {
        "key": "oppo_social_cn",
        "company_key": "oppo",
        "company_name": "OPPO",
        "display_name": "OPPO社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "OPPO公开社会招聘岗位",
        "base_url": "https://career.oppo.com",
        "connector": OppoConnector,
        "channels": {
            Channel.EXPERIENCED: "OPPO社会招聘公开岗位",
        },
    },
    "vivo": {
        "key": "vivo_social_cn",
        "company_key": "vivo",
        "company_name": "vivo",
        "display_name": "vivo社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "vivo公开社会招聘岗位",
        "base_url": "https://hr.vivo.com",
        "connector": VivoConnector,
        "channels": {
            Channel.EXPERIENCED: "vivo社会招聘公开岗位",
        },
    },
    "xiaohongshu": {
        "key": "xiaohongshu_social_cn",
        "company_key": "xiaohongshu",
        "company_name": "小红书",
        "display_name": "小红书社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "小红书公开社会招聘岗位",
        "base_url": "https://job.xiaohongshu.com",
        "connector": XiaohongshuConnector,
        "channels": {
            Channel.EXPERIENCED: "小红书社会招聘公开岗位",
        },
    },
    "iqiyi": {
        "key": "iqiyi_cn",
        "company_key": "iqiyi",
        "company_name": "爱奇艺",
        "display_name": "爱奇艺招聘",
        "source_type": "company_career_portal",
        "scope_name": "爱奇艺公开社会招聘与校园招聘岗位",
        "base_url": "https://careers.iqiyi.com",
        "connector": IqiyiConnector,
        "channels": {
            Channel.EXPERIENCED: "爱奇艺社会招聘公开岗位",
            Channel.CAMPUS: "爱奇艺校招页公开岗位（含正式与日常实习）",
        },
    },
    "cainiao": {
        "key": "alibaba_cainiao_social",
        "company_key": "cainiao",
        "company_name": "菜鸟集团",
        "display_name": "菜鸟集团社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "菜鸟集团公开社会招聘岗位",
        "base_url": "https://talent.cainiao.com",
        "connector": CainiaoConnector,
        "channels": {
            Channel.EXPERIENCED: "菜鸟集团社会招聘公开岗位",
        },
    },
    "alibaba-international": {
        "key": "alibaba_international_social",
        "company_key": "alibaba_international",
        "company_name": "阿里国际数字商业集团",
        "display_name": "阿里国际社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "阿里国际公开社会招聘岗位",
        "base_url": "https://aidc-jobs.alibaba.com",
        "connector": AlibabaInternationalConnector,
        "channels": {
            Channel.EXPERIENCED: "阿里国际社会招聘公开岗位",
        },
    },
    "aliyun": {
        "key": "alibaba_cloud_social",
        "company_key": "alibaba_cloud",
        "company_name": "阿里云",
        "display_name": "阿里云社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "阿里云公开社会招聘岗位",
        "base_url": "https://careers.aliyun.com",
        "connector": AliyunConnector,
        "channels": {
            Channel.EXPERIENCED: "阿里云社会招聘公开岗位",
        },
    },
    "beike": {
        "key": "beike_social_cn",
        "company_key": "beike",
        "company_name": "贝壳",
        "display_name": "贝壳社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "贝壳公开社会招聘岗位",
        "base_url": "https://join.ke.com",
        "connector": BeikeConnector,
        "channels": {
            Channel.EXPERIENCED: "贝壳社会招聘公开岗位",
        },
    },
    "tongcheng": {
        "key": "tongcheng_social_cn",
        "company_key": "tongcheng",
        "company_name": "同程旅行",
        "display_name": "同程旅行社会招聘",
        "source_type": "company_career_portal",
        "scope_name": "同程旅行公开社会招聘岗位",
        "base_url": "https://mhr.ly.com",
        "connector": TongchengConnector,
        "channels": {
            Channel.EXPERIENCED: "同程旅行社会招聘公开岗位",
        },
    },
    "lenovo": {
        "key": "lenovo_campus_cn",
        "company_key": "lenovo",
        "company_name": "联想集团",
        "display_name": "联想中国校园招聘",
        "source_type": "company_campus_portal",
        "scope_name": "联想中国公开应届生与人才项目岗位",
        "base_url": "https://talent.lenovo.com.cn",
        "connector": LenovoConnector,
        "channels": {
            Channel.CAMPUS: "联想中国应届生招聘与人才项目公开岗位",
        },
    },
}


def category_summary(result: CollectionResult) -> dict[str, object]:
    method_counts = Counter(
        category.assignment_method.value
        for job in result.jobs
        for category in job.categories
    )
    classified = sum(bool(job.categories) for job in result.jobs)
    return {
        "classified_jobs": classified,
        "unclassified_jobs": len(result.jobs) - classified,
        "multi_category_jobs": sum(len(job.categories) > 1 for job in result.jobs),
        "category_assignments": sum(len(job.categories) for job in result.jobs),
        "assignment_methods": dict(sorted(method_counts.items())),
    }


async def close_browser_stack(
    context: Any | None,
    browser: Any | None,
    playwright: Any | None,
) -> list[str]:
    """Best-effort cleanup that preserves the collection's real outcome."""

    errors: list[str] = []
    for label, resource, method_name in (
        ("context", context, "close"),
        ("browser", browser, "close"),
        ("playwright", playwright, "stop"),
    ):
        if resource is None:
            continue
        try:
            await getattr(resource, method_name)()
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-market")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create the database schema and seed sources")
    subparsers.add_parser(
        "check-data",
        help="Validate cross-table invariants and exit non-zero on violations",
    )
    subparsers.add_parser(
        "check-schema",
        help="Validate migration revision, model drift, and analysis views",
    )
    subparsers.add_parser(
        "check-runtime",
        help="Validate writable runtime storage has sufficient free space",
    )
    source_health = subparsers.add_parser(
        "check-source-health",
        help="Validate every active source channel is recent and succeeding",
    )
    source_health.add_argument("--max-age-hours", type=int)

    list_sources = subparsers.add_parser(
        "list-sources",
        help="List configured source aliases for deployment orchestration",
    )
    list_sources.add_argument(
        "--format",
        choices=["json", "lines", "summary"],
        default="json",
    )
    list_sources.add_argument(
        "--due-only",
        action="store_true",
        help="Only list sources missing a standard snapshot for today",
    )

    recover_runs = subparsers.add_parser(
        "recover-runs",
        help="Mark abandoned running crawl records as failed",
    )
    recover_runs.add_argument("--source", choices=sorted(SOURCE_SPECS))
    recover_runs.add_argument("--older-than-minutes", type=int)

    crawl = subparsers.add_parser("crawl", help="Collect public job data from a career site")
    crawl.add_argument(
        "--source",
        choices=sorted(SOURCE_SPECS),
        default="bytedance",
        help="Career site to collect",
    )
    crawl.add_argument(
        "--channel",
        choices=[channel.value for channel in Channel] + ["all"],
        default=Channel.CAMPUS.value,
    )
    crawl.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate without writing raw files or the database",
    )
    crawl.add_argument(
        "--due-only",
        action="store_true",
        help="Skip source channels that already have today's standard snapshot",
    )
    dry_run_scope = crawl.add_mutually_exclusive_group()
    dry_run_scope.add_argument(
        "--max-pages",
        type=int,
        help="Limit fetched pages; only valid together with --dry-run",
    )
    dry_run_scope.add_argument(
        "--full",
        action="store_true",
        help="Fetch the complete source without persistence; requires --dry-run",
    )
    crawl.add_argument(
        "--timeout-seconds",
        type=int,
        help="Maximum runtime for each source channel",
    )
    return parser


async def crawl(args: argparse.Namespace, settings: Settings) -> int:
    if args.max_pages is not None and not args.dry_run:
        raise ValueError("--max-pages is only allowed together with --dry-run")
    if args.full and not args.dry_run:
        raise ValueError("--full is only allowed together with --dry-run")
    if args.max_pages is not None and args.max_pages < 1:
        raise ValueError("--max-pages must be at least 1")
    due_only = getattr(args, "due_only", False)
    if due_only and args.dry_run:
        raise ValueError("--due-only cannot be combined with --dry-run")
    timeout_seconds = (
        args.timeout_seconds
        if args.timeout_seconds is not None
        else settings.crawl_channel_timeout_seconds
    )
    if timeout_seconds < 60:
        raise ValueError("--timeout-seconds must be at least 60")

    spec = SOURCE_SPECS[args.source]
    channels = (
        list(spec["channels"])
        if args.channel == "all"
        else [Channel(args.channel)]
    )
    unsupported = [channel for channel in channels if channel not in spec["channels"]]
    if unsupported:
        names = ", ".join(channel.value for channel in unsupported)
        raise ValueError(f"Source {args.source!r} does not support channel(s): {names}")
    max_pages = (
        None
        if args.full or not args.dry_run
        else (args.max_pages if args.max_pages is not None else 1)
    )

    repository: Repository | None = None
    source_id: int | None = None
    if not args.dry_run:
        engine = make_engine(settings)
        create_schema(engine)
        repository = Repository(engine, settings.missing_runs_before_close)
        source_id = repository.ensure_source(
            key=spec["key"],
            company_key=spec["company_key"],
            company_name=spec["company_name"],
            base_url=spec["base_url"],
            display_name=spec["display_name"],
            source_type=spec["source_type"],
            scope_name=spec["scope_name"],
            channels={
                channel.value: note for channel, note in spec["channels"].items()
            },
        )
        if due_only:
            due_channels = repository.due_source_channels().get(spec["key"], set())
            channels = [
                channel for channel in channels if channel.value in due_channels
            ]
            if not channels:
                print(
                    json.dumps(
                        {
                            "source": args.source,
                            "status": "skipped",
                            "reason": "all_channels_already_collected_today",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0

    failures: list[str] = []
    for channel in channels:
        run_id = "dry-run"
        connector = None
        context = None
        browser = None
        playwright = None
        network_metrics: BrowserNetworkMetrics | None = None
        try:
            if repository is not None and source_id is not None:
                run_id = repository.start_run(source_id, channel.value)
            raw_store = (
                None
                if args.dry_run
                else RawStore(settings.raw_data_dir, run_id, spec["key"])
            )

            # Keep the browser lifecycle inside the channel boundary. A Chromium
            # crash, missing dependency, or context failure must be recorded for
            # this channel while allowing the next channel to run. Stop Playwright
            # only after its browser and context have been closed.
            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(headless=settings.headless)
                context = await browser.new_context(
                    locale="zh-CN",
                    service_workers=(
                        "block" if settings.crawl_block_service_workers else "allow"
                    ),
                )
                network_metrics = BrowserNetworkMetrics()
                if settings.crawl_block_nonessential_resources:
                    await network_metrics.install_policy(context)
                page = await context.new_page()
                await network_metrics.attach_page(page)
                network_metrics.watch_new_pages(context)
                connector = spec["connector"](page, settings, raw_store)
                try:
                    async with asyncio.timeout(timeout_seconds):
                        result = await connector.collect(
                            channel,
                            max_pages=max_pages,
                        )
                except TimeoutError as exc:
                    raise RuntimeError(
                        f"Source channel exceeded {timeout_seconds} seconds"
                    ) from exc
                summary = {
                    "run_id": run_id,
                    "source": args.source,
                    "channel": channel.value,
                    "jobs": len(result.jobs),
                    "pages": result.pages_fetched,
                    "partitions": result.partition_counts,
                    "complete": result.complete,
                    "absence_authoritative": result.absence_authoritative,
                    "dry_run": args.dry_run,
                    "timeout_seconds": timeout_seconds,
                    "network": await network_metrics.snapshot(),
                }
                if result.complete:
                    summary["category_summary"] = category_summary(result)
                if repository is not None:
                    if not result.complete:
                        raise RuntimeError("Refusing to persist an incomplete collection")
                    summary["database"] = repository.ingest(run_id, result)
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            finally:
                cleanup_errors = await close_browser_stack(context, browser, playwright)
                if cleanup_errors:
                    print(
                        json.dumps(
                            {
                                "source": args.source,
                                "channel": channel.value,
                                "cleanup_warnings": cleanup_errors,
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                    )
        except Exception as exc:
            failures.append(f"{channel.value}: {exc}")
            if repository is not None and run_id != "dry-run":
                repository.fail_run(
                    run_id,
                    traceback.format_exc(),
                    [] if connector is None else connector.snapshots,
                )
            error_payload = {
                "run_id": run_id,
                "source": args.source,
                "channel": channel.value,
                "error": str(exc),
            }
            if network_metrics is not None:
                error_payload["network"] = await network_metrics.snapshot()
            print(
                json.dumps(error_payload, ensure_ascii=False),
                file=sys.stderr,
            )

    if failures:
        print(json.dumps({"failures": failures}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings()

    if args.command == "init-db":
        engine = make_engine(settings)
        create_schema(engine)
        repository = Repository(engine, settings.missing_runs_before_close)
        source_ids = {
            name: repository.ensure_source(
                key=spec["key"],
                company_key=spec["company_key"],
                company_name=spec["company_name"],
                base_url=spec["base_url"],
                display_name=spec["display_name"],
                source_type=spec["source_type"],
                scope_name=spec["scope_name"],
                channels={
                    channel.value: note
                    for channel, note in spec["channels"].items()
                },
            )
            for name, spec in SOURCE_SPECS.items()
        }
        print(json.dumps({"status": "ok", "source_ids": source_ids}, sort_keys=True))
        return

    if args.command == "list-sources":
        names = list(SOURCE_SPECS)
        if args.due_only:
            engine = make_engine(settings)
            create_schema(engine)
            due_keys = Repository(
                engine,
                settings.missing_runs_before_close,
            ).due_source_keys()
            names = [name for name in names if SOURCE_SPECS[name]["key"] in due_keys]
        if args.format == "lines":
            for name in names:
                print(name)
        elif args.format == "summary":
            print(
                json.dumps(
                    {
                        "companies": len(
                            {SOURCE_SPECS[name]["company_key"] for name in names}
                        ),
                        "sources": len(names),
                        "channels": sum(
                            len(SOURCE_SPECS[name]["channels"]) for name in names
                        ),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(
                json.dumps(
                    [
                        {
                            "name": name,
                            "source_key": SOURCE_SPECS[name]["key"],
                            "channels": [
                                channel.value
                                for channel in SOURCE_SPECS[name]["channels"]
                            ],
                        }
                        for name in names
                    ],
                    ensure_ascii=False,
                )
            )
        return

    if args.command == "check-runtime":
        report = RuntimeChecker(
            settings.raw_data_dir,
            minimum_free_gib=settings.raw_min_free_gib,
        ).run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "recover-runs":
        older_than_minutes = (
            args.older_than_minutes
            if args.older_than_minutes is not None
            else settings.abandoned_run_after_minutes
        )
        if older_than_minutes < 0:
            parser.error("--older-than-minutes cannot be negative")
        engine = make_engine(settings)
        create_schema(engine)
        source_key = (
            SOURCE_SPECS[args.source]["key"] if args.source is not None else None
        )
        recovered = Repository(
            engine,
            settings.missing_runs_before_close,
        ).fail_abandoned_runs(
            older_than=timedelta(minutes=older_than_minutes),
            source_key=source_key,
        )
        print(json.dumps({"recovered_runs": recovered}, sort_keys=True))
        return

    if args.command == "crawl":
        try:
            raise SystemExit(asyncio.run(crawl(args, settings)))
        except ValueError as exc:
            parser.error(str(exc))

    if args.command == "check-data":
        engine = make_engine(settings)
        create_schema(engine)
        report = DataQualityChecker(engine).run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "check-source-health":
        max_age_hours = (
            args.max_age_hours
            if args.max_age_hours is not None
            else settings.source_stale_after_hours
        )
        if max_age_hours < 1:
            parser.error("--max-age-hours must be positive")
        engine = make_engine(settings)
        create_schema(engine)
        report = SourceHealthChecker(
            engine,
            stale_after=timedelta(hours=max_age_hours),
        ).run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "check-schema":
        engine = make_engine(settings)
        report = check_schema(engine)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if not report["ok"]:
            raise SystemExit(1)
        return


if __name__ == "__main__":
    main()
