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
from newsbot.core.config import AgentSection, Secrets
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger

logger = get_logger(__name__)

TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30}
# 搜素引擎会拦默认 UA；用一个常见浏览器标识
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BING_ENDPOINT = "https://www.bing.com/search"
# 订阅源兜底列表：只放实测可直连、且给出真实文章地址的源
# （36Kr 与机器之心的 /feed 在本环境返回 0 条，故未收录）
DEFAULT_FEEDS = (
    "https://www.qbitai.com/feed",
    "https://www.infoq.cn/feed",
    "https://www.ifanr.com/feed",
    "https://www.solidot.org/index.rss",
    "https://techcrunch.com/feed/",
    "https://hnrss.org/frontpage",
)
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

# 检索词切分：标点一律当分隔符，因为中文没有词边界
_KEYWORD_SPLIT = re.compile(r"[，。！？、；：（）「」【】《》,\.!\?;:()\[\]\"'“”‘’]+")
# 功能词：它们不携带检索信息，却会让"多词同时命中"这条判据永远失败。
# 实测整句问话被当成关键词时，输出里会出现「订阅源里没有同时包含『新闻、
# 今天、重要』的条目」——用户读到会以为什么都没搜到。
STOPWORDS = frozenset(
    {
        "今天",
        "今日",
        "昨天",
        "明天",
        "最近",
        "最新",
        "目前",
        "现在",
        "重要",
        "有哪些",
        "哪些",
        "什么",
        "怎么",
        "如何",
        "多少",
        "是否",
        "请",
        "帮我",
        "给出",
        "汇总",
        "总结",
        "整理",
        "梳理",
        "列出",
        "要点",
        "摘要",
        "来源",
        "链接",
        "编号",
        "引用",
        "标注",
        "说明",
        "新闻",
        "资讯",
        "消息",
        "报道",
        "动态",
        "相关",
        "关于",
        "一下",
        "一些",
        "一个",
        "以及",
        "同时",
        "的",
        "了",
        "吗",
        "呢",
        "和",
        "与",
    }
)
# 只在词内部剥离多字功能词：单字功能词（的、和、与）会咬坏「目的地」「和平」
# 这类真正的词，所以整片段等于它时才算功能词；片段首尾的单字功能词单独收尾处理
_INNER_STOPWORDS = tuple(sorted((word for word in STOPWORDS if len(word) > 1), key=len, reverse=True))
_EDGE_CHARS = "的了和与吗呢"
# 长于这个长度的中文词按 3 字窗口切片段参与匹配：没有分词器时，「具身智能机器人
# 产业」整串永远不可能出现在标题里，但其中「机器人」应当能命中
LONG_WORD_CHARS = 4


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


def _phrases(query: str) -> list[str]:
    """剥掉功能词后剩下的内容词，用于判断"哪几个词没被命中"。

    整句问话直接当关键词会让"今天""重要""有哪些"参与匹配：多词同时命中的判据
    必然失败，输出里还会出现「订阅源里没有同时包含『新闻、今天、重要』的条目」，
    让人误以为一篇都没搜到。这里先按标点切、再在片段内部剥掉多字功能词，最后
    丢掉纯数字与单字残渣。筛完什么都不剩时退回按标点切的原片段，保证不比以前差。
    """
    cleaned = _KEYWORD_SPLIT.sub(" ", query)
    parts = [part.strip() for part in cleaned.split() if part.strip()]
    phrases: list[str] = []
    for part in parts:
        core = part
        for word in _INNER_STOPWORDS:
            core = core.replace(word, " ")
        for chunk in core.split():
            # 片段首尾的单字功能词（「的人工智能」）要去掉，但不碰「目的地」这类词：
            # 只有长度大于 2 才允许裁剪，裁完至少还剩两个字
            if len(chunk) > 2:
                chunk = chunk.strip(_EDGE_CHARS)
            if len(chunk) > 1 and chunk.lower() not in STOPWORDS:
                phrases.append(chunk)
    return phrases or [part for part in parts if len(part) > 1] or [query.strip()]


def _keywords(query: str) -> list[str]:
    """打分用关键词：内容词再加上长词的 3 字窗口。

    没有分词器时「具身智能机器人产业」这种整串不可能出现在任何标题里，切成窗口
    后「机器人」「智能机」这类片段才有机会命中。窗口只用于打分排序，不用于判断
    哪个词没命中——否则提示语会被一堆无意义片段淹没。
    """
    words: list[str] = []
    for phrase in _phrases(query):
        words.append(phrase)
        if len(phrase) > LONG_WORD_CHARS and phrase.isascii() is False:
            words.extend(phrase[index : index + 3] for index in range(len(phrase) - 2))
    return words


def _score(item: dict[str, str], query: str, keywords: list[str]) -> int:
    """相关度打分：整串命中权重最高，命中关键词越多分越高。

    只判断"是否命中任一词"会把「AI 芯片」退化成匹配「AI」，
    实测会返回一堆只沾了 AI 的无关条目，所以必须按命中数排序。
    """
    title = item["title"].lower()
    snippet = item["snippet"].lower()
    phrase = query.strip().lower()
    score = 0
    if phrase and phrase in title:
        score += 4
    elif phrase and phrase in snippet:
        score += 2
    for word in keywords:
        if word in title:
            score += 2
        elif word in snippet:
            score += 1
    return score


def match_feed_items(items: list[dict[str, str]], query: str, limit: int) -> tuple[list[dict[str, str]], str]:
    """按相关度过滤、用发布时间作次级排序；返回（结果, 需要如实告知用户的提示）。

    提示很重要：这些源是"最新内容流"而不是搜索引擎索引，常常只命中部分关键词
    （实测「AI 芯片」在没有芯片新闻时只能命中「AI」），不说明就会让用户误以为
    结果全都是关于该话题的。
    """
    phrases = _phrases(query)
    keywords = [word.lower() for word in _keywords(query) if word.strip()]
    scored = [(item, _score(item, query, keywords)) for item in items]
    matched = [(item, score) for item, score in scored if score > 0]
    pool = matched or [(item, 0) for item in items]
    pool.sort(key=lambda pair: (pair[1], pair[0]["published"] or ""), reverse=True)

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item, _ in pool:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
        if len(unique) >= limit:
            break

    if not matched:
        return unique, "订阅源里没有匹配该关键词的条目，以下是各源最新内容："
    hit_words = {
        word for word in phrases if any(word.lower() in (i["title"] + i["snippet"]).lower() for i, _ in matched)
    }
    missing = [word for word in phrases if word not in hit_words]
    if missing and len(phrases) > 1:
        return unique, f"订阅源里没有同时包含「{'、'.join(missing)}」的条目，以下是只匹配到其余关键词的内容："
    return unique, ""


class SearchTool:
    """按配置选择搜索引擎，统一返回可读文本与来源。"""

    name = "search"
    description = "搜索互联网，返回标题、链接和摘要。了解最新信息时先用它，再按需抓取正文。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词，2～5 个词（例如「具身智能 融资」）；不要传整句问话，也不要用「今天/重要/有哪些」这类无检索价值的词",
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
        feeds: list[str] | None = None,
        feed_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._feeds = list(feeds if feeds is not None else DEFAULT_FEEDS)
        self._feed_concurrency = max(1, feed_concurrency)
        self._timeout = timeout
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
            parts = [f"{index}. {hit['title']}", f"   {hit['url']}"]
            if snippet.strip():
                parts.append(f"   {snippet}")
            blocks.append("\n".join(parts))
        sources = [Source(title=hit["title"], url=hit["url"]) for hit in hits]
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

    async def _feeds_provider(self, query: str) -> tuple[list[dict[str, str]], str]:
        """并发拉取订阅源，按关键词过滤：来源是筛选过的质量源，且给出真实文章地址。"""
        if not self._feeds:
            logger.warning("未配置任何订阅源，feeds 提供方无法工作")
            return [], ""
        gate = asyncio.Semaphore(self._feed_concurrency)

        async def fetch(url: str) -> list[dict[str, str]]:
            async with gate:
                try:
                    response = await self._http().get(url, headers={"User-Agent": USER_AGENT})
                    response.raise_for_status()
                except Exception as exc:  # 单个源失败不该拖垮整次检索
                    logger.warning("订阅源拉取失败 %s: %s", url, exc)
                    return []
                return parse_feed(response.text, _host(url))

        batches = await asyncio.gather(*(fetch(url) for url in self._feeds))
        hits, note = match_feed_items(
            [item for batch in batches for item in batch], query, self._settings.search_results
        )
        if not hits:
            logger.warning("订阅源没有返回任何条目（共 %d 个源）", len(self._feeds))
            return [], ""
        if note:
            logger.info("订阅源检索提示: %s", note)
        return hits, note

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
