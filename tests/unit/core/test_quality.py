"""
模块: tests.unit.core.test_quality
职责: 校验来源质量判定——列表页 / 搜索页 / 抽取失败的标题不进资讯库
依赖: core.quality
"""

from __future__ import annotations

import pytest

from newsbot.core import quality

# 库里 158 条真实数据里人工逐条确认的 11 条脏数据（标题, 网址）
DIRTY = [
    ("Trending  repositories on GitHub today · GitHub", "https://github.com/trending"),
    ("华尔街见闻 - 7×24小时全球财经资讯与金融市场直播", "https://wallstreetcn.com/"),
    ("股票频道 - 东方财富网", "https://stock.eastmoney.com/"),
    ("全球财经快讯_东方财富网", "https://kuaixun.eastmoney.com/"),
    ("财经滚动新闻_新浪网", "https://finance.sina.com.cn/roll/"),
    (
        "新华保险 举牌 国泰海通 - 搜索",
        "https://www.bing.com/search?q=%E6%96%B0%E5%8D%8E%E4%BF%9D%E9%99%A9+%E4%B8%BE%E7%89%8C",
    ),
    ("公司资讯 _ 东方财富网", "https://finance.eastmoney.com/a/cgsxw.html"),
    ("AI News & Artificial Intelligence | TechCrunch", "https://techcrunch.com/category/artificial-intelligence/"),
    ("Artificial Intelligence | The Verge", "https://www.theverge.com/ai-artificial-intelligence"),
    ("https://www.bing.com/search?q=apt+%E5%88%9B%E5%A7%8B%E4%BA%BA", "https://www.bing.com/search?q=apt"),
    ("https://www.bing.com/news/search?q=Sam+Altman", "https://www.bing.com/news/search?q=Sam+Altman"),
]

# 同一批数据里的正常文章，一条都不能误伤
GOOD = [
    (
        "Microsoft AI Releases Microsoft-Decision-1: A Qwen3.5-9B Decision-Scoring Model",
        "https://www.marktechpost.com/2026/10/09/microsoft-ai-releases-microsoft-decision-1/",
    ),
    ("FAST 发现一个脉冲星三星系统", "https://www.solidot.org/story?sid=85583"),
    ("华为AI靠RSI通关25款游戏！操作次数比人类少56%", "https://www.qbitai.com/2026/10/502654.html"),
    (
        "Dev used AI agents and more than 500 billion tokens to rebuild Call of Duty",
        "https://www.tomshardware.com/tech-industry/artificial-intelligence/dev-used-ai-agents",
    ),
    (
        "Meta ZGateway 在每秒处理超过 10 亿次操作的同时，将 ZippyDB 的连接数减少了 19 倍",
        "https://www.infoq.cn/article/d5x9BkQ9AoIZq5mf6LN0",
    ),
    ("Startup Funding: Q3 2026", "https://semiengineering.com/startup-funding-q3-2026/"),
    (
        "A New Anti-Aging Drug Looks to Turn Back the Clock on Skin Age",
        "https://www.wired.com/story/new-anti-aging-drug-trial-for-skin/",
    ),
    # 编号型 slug：整段是数字，看着不像"文章形状"，但确实是文章
    ("早报｜苹果一大波新品曝光", "https://www.ifanr.com/1683053"),
    # 标题里有连字符的产品名：分隔符前后没空格，不能当站点名
    ("MiMo-V2.6 发布", "https://example.com/mimo-v26"),
    ("GPT-6.1 Sol 极速版上线", "https://example.com/gpt61"),
    # 项目主页：标题有实际内容，判掉会丢真来源（这是刻意不采用"首页即列表页"的原因）
    ("Nitter: Update Oct 10th seeking funding and legal help", "https://nitter.net"),
    ("IRCv3", "https://ircv3.net/"),
]


@pytest.mark.parametrize(("title", "url"), DIRTY)
def test_detects_listing_pages(title: str, url: str) -> None:
    assert quality.is_listing(title, url), f"应判为列表页/搜索页：{title!r}"


@pytest.mark.parametrize(("title", "url"), GOOD)
def test_keeps_real_articles(title: str, url: str) -> None:
    assert not quality.is_listing(title, url), f"误伤了正常文章：{title!r}"


def test_separator_requires_leading_whitespace() -> None:
    """分隔符前面必须有空白，这是整条规则零误伤的关键。"""
    assert quality.is_listing_title("股票频道 - 东方财富网")
    assert not quality.is_listing_title("MiMo-V2.6")
    assert not quality.is_listing_title("Microsoft-Decision-1 发布")
    assert not quality.is_listing_title("GPT-6")


def test_title_tail_excludes_sentence_punctuation() -> None:
    """尾部有句子标点说明那是半句话，不是站点名。"""
    assert not quality.is_listing_title("某公司发布芯片，性能提升三成，这是标题的后半句")


def test_bare_url_title() -> None:
    assert quality.is_bare_url_title("https://www.bing.com/news/search?q=x")
    assert not quality.is_bare_url_title("Scaling laws 的新证据")


def test_search_url_only_for_query_pages() -> None:
    """判的是搜索结果页，不是"这个域名下的任何页面"——引擎自己的新闻文章不能中枪。"""
    assert quality.is_search_url("https://www.bing.com/search?q=apt")
    assert quality.is_search_url("https://www.google.com/search?q=apt")
    assert not quality.is_search_url("https://www.bing.com/news/article/123")
    assert not quality.is_search_url("https://example.com/search")


def test_listing_url_matches_segment_wordlist_only() -> None:
    """只认词表。`semiengineering.com/startup-funding-q3-2026/` 也是单段且以 / 结尾，
    但那是正常文章，靠"形状"区分不了。"""
    assert quality.is_listing_url("https://github.com/trending")
    assert quality.is_listing_url("https://finance.sina.com.cn/roll/")
    assert quality.is_listing_url("https://techcrunch.com/category/artificial-intelligence/")
    assert not quality.is_listing_url("https://semiengineering.com/startup-funding-q3-2026/")
    assert not quality.is_listing_url("https://example.com/categories-of-thought")


def test_is_listing_is_the_union() -> None:
    """组合判据是四条规则的并集，任一条命中即成立。"""
    assert quality.is_listing("股票频道 - 东方财富网", "https://anything.example/x")
    assert quality.is_listing("正常标题", "https://www.bing.com/search?q=x")
    assert not quality.is_listing("正常标题", "https://example.com/2026/10/1.html")
