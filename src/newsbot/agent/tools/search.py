"""
模块: agent.tools.search
职责: 搜索工具——SearXNG / Tavily / Bing(HTML) / RSS 订阅源统一为标题 / 链接 / 摘要
依赖: agent.tools.base, core.config, core.errors, core.log
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import html as html_mod
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection, FeedSource, Secrets, default_feeds
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger

logger = get_logger(__name__)

TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30}
# 搜素引擎会拦默认 UA；用一个常见浏览器标识
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BING_ENDPOINT = "https://www.bing.com/search"
# 单个订阅源的最长等待：hnrss 实测会挂 20～36 秒，而检索必须等所有源返回，
# 不给单源设上限就会让一次搜索白等半分钟
DEFAULT_FEED_TIMEOUT = 8.0
# Bing 结果块；旧结构用 li.b_algo，同时兼容 b_algo 出现在 div 上的情况
_BING_BLOCK = re.compile(
    r'<(?:li|div)[^>]*class="[^"]*\bb_algo\b[^"]*".*?(?=<(?:li|div)[^>]*class="[^"]*\bb_algo\b|</ol>)', re.S
)
_BING_LINK = re.compile(r'<a[^>]+href="(https?://[^"]+)"', re.S)
_BING_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Bing 有时把结果包成跳转：https://www.bing.com/ck/a?...&u=a1<base64url>
_REDIRECT = re.compile(r"[?&]u=a1([A-Za-z0-9_\-=]+)")


def _clean(text: str) -> str:
    """去标签、解 HTML 实体、压空白——摘要里常混着 &ensp; 与 &#0183; 这类实体。"""
    return _WS.sub(" ", html_mod.unescape(_TAG.sub(" ", text))).strip()


# 摘要长度低于这个值就没有信息量，不如留空让 Agent 直接抓正文。
# 中文信息密度高，阈值取得比对英文低（英文摘要普遍上百字符）
MIN_SNIPPET_CHARS = 12
# 输出给 Agent 的摘要上限：实测多轮搜索会把上下文撑爆 token 预算，
# 摘要只是"要不要抓这篇"的判断依据，细节交给 fetch
SNIPPET_OUTPUT_CHARS = 140
# 各源在 description 里夹带的推广尾巴（爱范儿等），保留它只会污染日报
_PROMO = re.compile(r"#?\s*欢迎关注.{0,60}?(?:微信公众号|公众号|微信号)[^\n]*", re.S)
_BOILERPLATE = re.compile(r"^(?:点击)?(?:查看|阅读)?(?:原文|详情|全文)[>》]?$")
# 营销/导购条目：Wired 的 RSS 里实测 10 条有 6 条是优惠券导购（"LG Promo Codes…"、
# "30% Off Canon…"），Tom's Hardware 也有 "Save $200 on… amazon deal" 这类。
# 它们没有任何资讯价值，留着只会挤掉真正的内容。
_LOW_QUALITY = re.compile(
    r"promo\s*codes?|coupon|discount\s*codes?|优惠券|折扣码|\bsave \$?\d|amazon deal|best deals?\b",
    re.I,
)

# 检索词切分：只按空白与标点切，不做任何"猜用户想查什么"的处理。
# 提炼检索词是 Agent 规划阶段的职责；工具猜不了的（猜中了也解释不清为什么），
# 一旦工具替模型猜，模型就收不到"检索词没写好"的反馈，规划能力永远长不出来。
_TOKEN_SPLIT = re.compile(r"\s+|[，。！？、；：（）「」【】《》,.!?;:()\[\]\"'“”‘’]+")


def _unwrap(url: str) -> str:
    """把 Bing 的跳转链接还原成目标地址；直链原样返回。"""
    if "bing.com/ck/a" not in url:
        return url
    match = _REDIRECT.search(url)
    if match is None:
        return url
    raw = match.group(1)
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw + padding).decode("utf-8", "replace")
    except (binascii.Error, ValueError):  # pragma: no cover - 上游编码异常
        return url
    return decoded if decoded.startswith("http") else url


def parse_bing(html: str, limit: int) -> list[dict[str, str]]:
    """把 Bing 结果页解析成统一结构；纯函数，便于离线测试。"""
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in _BING_BLOCK.findall(html):
        link = _BING_LINK.search(block)
        if link is None:
            continue
        url = _unwrap(html_mod.unescape(link.group(1)))
        if not url.startswith("http") or url in seen:
            continue
        title = _BING_TITLE.search(block)
        snippet = _BING_SNIPPET.search(block)
        seen.add(url)
        hits.append(
            {
                "title": _clean(title.group(1)) if title else url,
                "url": url,
                "snippet": _clean(snippet.group(1)) if snippet else "",
            }
        )
        if len(hits) >= limit:
            break
    return hits


# ── RSS / Atom 订阅源（SEARCH_PROVIDER=feeds）──
_RSS_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_ATOM_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TAG_STRIP = re.compile(r"<[^>]+>")
# 跟踪参数对引用毫无价值，去掉让来源更干净
_TRACKING_PARAMS = re.compile(r"[?&](utm_[^=&]+|from|oc|ref|spm)=[^&]*")


def _tag(block: str, name: str) -> str:
    """取第一个同名标签的文本，兼容 CDATA。

    顺序很重要：必须先解 HTML 实体、再剥标签。反过来时，
    `&lt;div&gt;` 这类转义标签会先被跳过、解码后又变成真的标签混进正文
    （InfoQ 的 description 就是这样，实测会输出一堆原始 HTML）。
    """
    match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    if match is None:
        return ""
    raw = match.group(1).strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
    if raw.endswith("]]>"):
        raw = raw[:-3]
    return _WS.sub(" ", _TAG_STRIP.sub(" ", html_mod.unescape(raw))).strip()


def _atom_link(block: str) -> str:
    match = re.search(r'<link[^>]+href="([^"]+)"', block)
    return html_mod.unescape(match.group(1)).strip() if match else ""


def _tidy_url(url: str) -> str:
    """去掉跟踪参数，避免引用里带一长串无意义后缀。"""
    return html_mod.unescape(_TRACKING_PARAMS.sub("", url)).rstrip("?&")


def _published(block: str) -> str:
    """把 pubDate / published / updated 归一成 ISO 字符串；解析不了就留空。"""
    for name in ("pubDate", "published", "updated", "dc:date"):
        raw = _tag(block, name)
        if not raw:
            continue
        stamp: datetime | None = None
        try:
            stamp = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                stamp = None
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat()
    return ""


def clean_snippet(text: str) -> str:
    """清掉摘要里的推广尾巴与"点击查看原文"这类样板；没信息量就返回空。

    实测各源差异极大：Solidot / TechCrunch 给整段摘要（好用），
    InfoQ 的 description 只有"点击查看原文>"，爱范儿则带一段公众号推广。
    """
    cleaned = _PROMO.sub("", text).replace("\u200b", "").strip()
    if not cleaned or _BOILERPLATE.match(cleaned):
        return ""
    return cleaned if len(cleaned) >= MIN_SNIPPET_CHARS else ""


def parse_feed(text: str, source: str) -> list[dict[str, str]]:
    """解析 RSS 2.0 与 Atom，产出统一结构；纯函数，便于离线测试。

    返回字段：title / url / snippet / published / source。
    """
    is_rss = bool(_RSS_ITEM.search(text))
    blocks = _RSS_ITEM.findall(text) if is_rss else _ATOM_ENTRY.findall(text)
    items: list[dict[str, str]] = []
    for block in blocks:
        url = _tidy_url(_tag(block, "link") if is_rss else _atom_link(block))
        if not url.startswith("http"):
            continue
        title = _tag(block, "title") or url
        # 导购/营销内容不是资讯：留着只会挤掉真正的内容（实测 Wired 的 feed 里占六成）
        if _LOW_QUALITY.search(title):
            continue
        summary = _tag(block, "description") or _tag(block, "summary") or _tag(block, "content")
        items.append(
            {
                "title": _tag(block, "title") or url,
                "url": url,
                "snippet": clean_snippet(summary)[:SNIPPET_OUTPUT_CHARS],
                "published": _published(block),
                "source": source,
            }
        )
    return items


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1).removeprefix("www.") if match else url


def query_tokens(query: str) -> list[str]:
    """把检索词按空白与标点切成 token。

    只做切分，不做任何"猜用户想查什么"的处理：不删功能词、不做同义改写、
    不切窗口。提炼检索词是 Agent 规划阶段的职责；工具替模型猜会同时弄坏两件事
    ——猜错了结果不可用，猜对了模型也学不到该怎么写检索词。
    实测证据：曾被整句问话触发「订阅源里没有同时包含『新闻、今天、重要』的条目」，
    正确做法是让模型看到这个反馈并改写检索词，而不是让工具偷偷把词删掉。
    """
    return [token.strip() for token in _TOKEN_SPLIT.split(query) if token.strip()]


def _hit(token: str, text: str) -> bool:
    """判断一个 token 是否真的出现在文本里。

    中文按子串判断（中文没有词边界）；ASCII 要求两侧不是字母数字，否则
    「AI」会命中 said / email / chain / openai——英文源里几乎每篇都含 said、email，
    等于把「AI」变成万能匹配。这是匹配正确性，不是意图猜测。
    """
    if token.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None
    return token in text


def _score(item: dict[str, Any], tokens: list[str]) -> float:
    """相关度：命中的 token 越多分越高；标题命中比摘要命中权重高。

    再乘来源权重：同一件事被多家报道时，权重高的先出现，去重阶段也就先把
    高质量的那条占住位置。权重只做**排序的次级因素**——相关度是主要依据，
    否则一个权威源只要沾一个词就会压过真正相关的条目。
    """
    title = item["title"].lower()
    snippet = item["snippet"].lower()
    score = 0
    for token in tokens:
        if _hit(token, title):
            score += 2
        elif _hit(token, snippet):
            score += 1
    return score * float(item.get("weight", 1.0))


def match_feed_items(items: list[dict[str, Any]], query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """按检索词过滤订阅源条目，并按相关度 → 发布时间排序；返回（结果, 反馈）。

    订阅源是"最新内容流"而不是搜索索引，常常只命中部分检索词。反馈必须如实说
    清楚**哪些词没命中**，这是 Agent 决定"改写检索词还是收手"的唯一依据——
    把没命中的词悄悄删掉，等于把反馈信号也删掉了。
    """
    tokens = [token.lower() for token in query_tokens(query)]
    scored = [(item, _score(item, tokens)) for item in items]
    matched = [(item, score) for item, score in scored if score > 0]
    pool = matched or [(item, 0.0) for item in items]
    pool.sort(key=lambda pair: (pair[1], pair[0]["published"] or ""), reverse=True)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item, _ in pool:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
        if len(unique) >= limit:
            break

    if not matched:
        return unique, "没有条目包含这些检索词中的任何一个，以下是各源最新内容（请改写检索词）："
    body = [(i["title"] + i["snippet"]).lower() for i, _ in matched]
    missing = [token for token in tokens if not any(_hit(token, text) for text in body)]
    if missing and len(tokens) > 1:
        return unique, f"这些检索词没有任何条目同时包含：{'、'.join(missing)}。以下条目只命中了其余的词："
    return unique, ""


class SearchTool:
    """按配置选择搜索引擎，统一返回可读文本与来源。"""

    name = "search"
    description = (
        "检索互联网，返回标题、链接与摘要。传检索词（空格分隔的核心词效果最好）；"
        "结果里会如实告诉你哪些词没有命中，据此决定是否改写检索词。"
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索词。工具按空白与标点切成词逐个匹配，整句问话里的虚词会匹配不到",
            },
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month"],
                "description": "限定时间范围，查最新资讯时使用",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        settings: AgentSection,
        secrets: Secrets,
        *,
        feeds: list[FeedSource] | None = None,
        feed_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        feed_timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._feeds = list(feeds) if feeds is not None else default_feeds()
        self._feed_concurrency = max(1, feed_concurrency)
        self._timeout = timeout
        # 单个源的最长等待：实测 hnrss 会挂 20～36 秒，而检索要等所有源返回，
        # 不给单源设上限就会让一次搜索白等半分钟。默认取总超时的 40%。
        self._feed_timeout = feed_timeout if feed_timeout is not None else min(timeout * 0.4, DEFAULT_FEED_TIMEOUT)
        self._client = client
        self._owns_client = client is None

    @property
    def provider(self) -> str:
        return self._secrets.search_provider.lower()

    async def run(self, query: str, time_range: str | None = None, **_: Any) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult.failure("缺少搜索关键词")
        try:
            hits, note = await self._collect(query, time_range)
        except httpx.TimeoutException as exc:
            raise RetriableError(f"搜索超时: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RetriableError(f"搜索返回 {exc.response.status_code}") from exc

        if not hits:
            return ToolResult(text="没有搜索到相关结果。")
        blocks = []
        for index, hit in enumerate(hits, 1):
            snippet = hit["snippet"][:SNIPPET_OUTPUT_CHARS]
            # 标出来源名：Agent 判断"这条够不够权威、要不要再找个一手来源"要靠它。
            # 必须放在标题行——`SourceRegistry.render` 靠"编号行 + 下一页纯网址行"
            # 这个结构把局部编号换成全局编号，网址行带上任何后缀都会让改写失效，
            # 正文引用与来源列表就会错位（实测踩过一次）
            origin = hit.get("source") or _host(hit["url"])
            parts = [f"{index}. {hit['title']}（{origin}）", f"   {hit['url']}"]
            if snippet.strip():
                parts.append(f"   {snippet}")
            blocks.append("\n".join(parts))
        sources = [
            Source(
                title=hit["title"],
                url=hit["url"],
                snippet=hit["snippet"],
                weight=float(hit.get("weight", 1.0)),
            )
            for hit in hits
        ]
        body = "\n".join(blocks)
        return ToolResult(text=f"{note}\n{body}".strip() if note else body, sources=sources)

    async def _collect(self, query: str, time_range: str | None) -> tuple[list[dict[str, str]], str]:
        """按配置选引擎；返回（结果, 需要如实告知用户的提示）。"""
        if self.provider == "tavily":
            return await self._tavily(query), ""
        if self.provider == "bing":
            return await self._bing(query), ""
        if self.provider == "feeds":
            return await self._feeds_provider(query)
        return await self._searxng(query, time_range), ""

    async def _feeds_provider(self, query: str) -> tuple[list[dict[str, Any]], str]:
        """并发拉取订阅源，按关键词过滤：来源是筛选过的质量源，且给出真实文章地址。"""
        if not self._feeds:
            logger.warning("未配置任何订阅源，feeds 提供方无法工作")
            return [], ""
        gate = asyncio.Semaphore(self._feed_concurrency)

        async def fetch(feed: FeedSource) -> list[dict[str, Any]]:
            async with gate:
                items = await self._fetch_feed(feed)
                # 权重挂在条目上，随结果一路带到排序与去重
                return [{**item, "weight": feed.weight} for item in items]

        batches = await asyncio.gather(*(fetch(feed) for feed in self._feeds))
        hits, note = match_feed_items(
            [item for batch in batches for item in batch], query, self._settings.search_results
        )
        if not hits:
            logger.warning("订阅源没有返回任何条目（共 %d 个源）", len(self._feeds))
            return [], ""
        if note:
            logger.info("订阅源检索提示: %s", note)
        return hits, note

    async def _fetch_feed(self, feed: FeedSource) -> list[dict[str, str]]:
        """拉取并解析单个订阅源；返回空列表表示这次没拿到。

        超时**不重试**：实测慢源（hnrss 实测挂 20～36 秒）会一直慢，重试只是让整次
        检索多等一个超时周期。连接类错误**重试一次**：连续检索时实测会出现"整批里
        几个源同时失败"，那是瞬时抖动，不该当成源不可用。
        """
        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    self._http().get(feed.url, headers={"User-Agent": USER_AGENT}), timeout=self._feed_timeout
                )
                response.raise_for_status()
            except (TimeoutError, httpx.TimeoutException):
                # 写成两个类型：`asyncio.wait_for` 抛的是内置 TimeoutError，而 httpx
                # 自己抛的 ReadTimeout / ConnectTimeout 与它没有继承关系（实测踩过：
                # 只捕获内置 TimeoutError 会让 httpx 超时被当成可重试的连接错误）
                logger.warning("订阅源超时 %s（上限 %.1fs），跳过", feed.url, self._feed_timeout)
                return []
            except Exception as exc:
                if attempt == 1:
                    await asyncio.sleep(0.3)
                    continue
                logger.warning("订阅源拉取失败 %s: %s: %s", feed.url, type(exc).__name__, exc)
                return []
            return parse_feed(response.text, _host(feed.url))
        return []

    async def _bing(self, query: str) -> list[dict[str, str]]:
        """抓 Bing 结果页并解析：零配置可用，代价是依赖页面结构。"""
        logger.info("使用内置 Bing 搜索（HTML 解析），关键词: %s", query)
        response = await self._http().get(
            BING_ENDPOINT,
            params={"q": query, "count": self._settings.search_results, "setlang": "zh-CN"},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        response.raise_for_status()
        hits = parse_bing(response.text, self._settings.search_results)
        if not hits:
            logger.warning("Bing 结果页解析为空，页面结构可能已变化（len=%d）", len(response.text))
        return hits

    async def _searxng(self, query: str, time_range: str | None) -> list[dict[str, str]]:
        params: dict[str, Any] = {"q": query, "format": "json"}
        if time_range in TIME_RANGE_DAYS:
            params["time_range"] = time_range
        response = await self._http().get(f"{self._secrets.searxng_url.rstrip('/')}/search", params=params)
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": str(item.get("title") or "无标题"),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "").strip(),
            }
            for item in (data.get("results") or [])[: self._settings.search_results]
        ]

    async def _tavily(self, query: str) -> list[dict[str, str]]:
        if not self._secrets.tavily_api_key:
            return []
        response = await self._http().post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._secrets.tavily_api_key,
                "query": query,
                "max_results": self._settings.search_results,
                "search_depth": "basic",
            },
        )
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": str(item.get("title") or "无标题"),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "").strip(),
            }
            for item in (data.get("results") or [])[: self._settings.search_results]
        ]

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
