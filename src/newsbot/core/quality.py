"""
模块: core.quality
职责: 来源质量判定——列表页 / 搜索结果页 / 抽取失败的标题不进资讯库与事件池
依赖: 无（纯函数，供 agent 与 dispatcher 共用）
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

# 栏目页 / 首页：RSS 与抓取结果里常混进"频道自己"。实测抓到的原文标题——
# 「股票频道 - 东方财富网」「公司资讯 _ 东方财富网」「全球财经快讯_东方财富网」
# 「财经滚动新闻_新浪网」「新华保险 举牌 国泰海通 - 搜索」
# 「AI News & Artificial Intelligence | TechCrunch」「Artificial Intelligence | The Verge」
# 「Trending  repositories on GitHub today · GitHub」
# 「华尔街见闻 - 7×24小时全球财经资讯与金融市场直播」。
# 它们不是文章：留着会挤掉真内容，还会被判成"同一件事"——实测两条这样的条目真的被
# 聚簇合成了一个"事件"，并占据热度榜首位。
#
# 分隔符**前面必须有空白**——这一条是整个规则零误伤的关键：`MiMo-V2.6`、
# `Microsoft-Decision-1`、`GPT-6` 里的连字符前后没有空格，都是正常文章标题，不能当
# 分隔符。尾部排除句子标点，避免把长句子当站点名。
# 该规则在库里 158 条真实标题上验证：8/8 召回（上面那些全部命中）、0 条文章被误伤。
LISTING_TITLE = re.compile(r"(?:\s[|\-·—]\s*|_\s*)[^。！？，,]{1,20}$")

# 标题本身是网址 = 抽取失败，条目没有任何可用信息（实测两条 bing 结果页如此）
_BARE_URL = re.compile(r"^https?://", re.I)

# 路径段整段是列表页词。**只认词表、不看形状**：`semiengineering.com/startup-funding-q3-2026/`
# 也是单段且以 `/` 结尾，但它是正常文章，靠形状区分不了。
LISTING_SEGMENTS = frozenset({"category", "categories", "tag", "tags", "topic", "topics", "roll", "trending"})

# 搜索页：搜索引擎域名 + 查询串。搜索结果页不是新闻，存下来只会污染事件池。
_SEARCH_HOSTS = ("bing.com", "google.com", "baidu.com", "duckduckgo.com", "sogou.com")
_QUERY_KEYS = ("q", "query", "wd", "keyword", "p")


def is_listing_title(title: str) -> bool:
    """标题是不是"频道 / 列表页"的名字。"""
    return LISTING_TITLE.search((title or "").strip()) is not None


def is_bare_url_title(title: str) -> bool:
    """标题是不是一个光秃秃的网址（抽取失败）。"""
    return _BARE_URL.match((title or "").strip()) is not None


def is_search_url(url: str) -> bool:
    """网址是不是搜索结果页。"""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in _SEARCH_HOSTS):
        return False
    query = parse_qs(parts.query)
    return any(key in query for key in _QUERY_KEYS) or parts.path.rstrip("/").endswith("/search")


def is_listing_url(url: str) -> bool:
    """网址的路径段里有没有列表页词。"""
    segments = [seg for seg in urlsplit(url or "").path.strip("/").split("/") if seg]
    return any(seg.lower() in LISTING_SEGMENTS for seg in segments)


def is_listing(title: str, url: str = "") -> bool:
    """这条来源是不是"不是文章"的页面。

    四条判据的并集在库里 158 条真实数据上**正好命中 11 条脏数据、147 条正常文章零误伤**。
    刻意**没有**加"首页即列表页"：实测它会连 `nitter.net`、`ircv3.net` 这类标题有实际
    内容的来源一起判掉，代价大于收益（宁可漏掉 `https://www.eastmoney.com/` 一条）。
    """
    return is_listing_title(title) or is_bare_url_title(title) or is_search_url(url) or is_listing_url(url)
