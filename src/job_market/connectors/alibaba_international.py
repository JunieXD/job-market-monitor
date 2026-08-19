"""Alibaba International public experienced-recruitment connector."""

from job_market.connectors.cainiao import CainiaoConnector


class AlibabaInternationalConnector(CainiaoConnector):
    """Use the shared Alibaba portal contract with an isolated source identity."""

    source_key = "alibaba_international_social"
    company_name = "阿里国际数字商业集团"
    portal_name = "Alibaba International"
    position_page_url = "https://aidc-jobs.alibaba.com/off-campus?lang=zh"
    position_url = (
        "https://aidc-jobs.alibaba.com/off-campus-position/{external_id}"
    )
    request_delay_setting = "alibaba_international_request_delay_seconds"
