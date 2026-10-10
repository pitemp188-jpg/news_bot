"""
模块: tests.unit.agent.test_search
职责: 校验 SearXNG / Tavily / Bing 调用、结果归一化、页面解析与错误分类
依赖: agent.tools.search
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
import respx

from newsbot.agent.tools.search import (
    BING_ENDPOINT,
    SearchTool,
    _keywords,
    _phrases,
    clean_snippet,
    match_feed_items,
    parse_bing,
    parse_feed,
)
from newsbot.core.config import AgentSection, Secrets
from newsbot.core.errors import RetriableError

SEARX = "http://127.0.0.1:8080/search"
TAVILY = "https://api.tavily.com/search"
BING_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "pages" / "bing_results.html"
FEED_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "pages" / "feed_rss.xml"


def _tool(provider: str = "searxng", **overrides: object) -> SearchTool:
    secrets = Secrets(search_provider=provider, tavily_api_key="tvly-test")
    settings = AgentSection(search_results=int(overrides.get("search_results", 5)))  # type: ignore[arg-type]
    return SearchTool(settings, secrets)


@respx.mock
async def test_searxng_normalizes_results() -> None:
    respx.get(SEARX).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "标题一", "url": "https://a.example/1", "content": "摘要一"},
                    {"title": "", "url": "https://a.example/2"},
                ]
            },
        )
    )
    tool = _tool()
    result = await tool.run("AI 新闻", time_range="day")
    assert "标题一" in result.text
    assert "无标题" in result.text
    assert [source.url for source in result.sources] == ["https://a.example/1", "https://a.example/2"]
    assert respx.calls[0].request.url.params["time_range"] == "day"
    await tool.aclose()


@respx.mock
async def test_searxng_limits_result_count() -> None:
    payload = {"results": [{"title": f"t{i}", "url": f"https://a.example/{i}", "content": "s"} for i in range(10)]}
    respx.get(SEARX).mock(return_value=httpx.Response(200, json=payload))
    tool = _tool(search_results=3)
    result = await tool.run("AI")
    assert len(result.sources) == 3
    await tool.aclose()


@respx.mock
async def test_tavily_is_used_when_configured() -> None:
    respx.post(TAVILY).mock(
        return_value=httpx.Response(200, json={"results": [{"title": "T", "url": "https://b.example", "content": "c"}]})
    )
    tool = _tool("tavily")
    result = await tool.run("AI")
    assert result.sources[0].url == "https://b.example"
    assert respx.calls[0].request.headers["content-type"] == "application/json"
    await tool.aclose()


async def test_empty_query_is_reported() -> None:
    tool = _tool()
    assert "缺少搜索关键词" in (await tool.run("   ")).text
    await tool.aclose()


@respx.mock
async def test_no_results_returns_plain_text() -> None:
    respx.get(SEARX).mock(return_value=httpx.Response(200, json={"results": []}))
    tool = _tool()
    result = await tool.run("冷门词")
    assert result.text == "没有搜索到相关结果。"
    assert result.sources == []
    await tool.aclose()


@respx.mock
async def test_server_error_is_retriable() -> None:
    respx.get(SEARX).mock(return_value=httpx.Response(500))
    tool = _tool()
    with pytest.raises(RetriableError):
        await tool.run("AI")
    await tool.aclose()


@respx.mock
async def test_timeout_is_retriable() -> None:
    respx.get(SEARX).mock(side_effect=httpx.ConnectTimeout("慢"))
    tool = _tool()
    with pytest.raises(RetriableError):
        await tool.run("AI")
    await tool.aclose()


# ── 内置 Bing 搜索（HTML 解析，零配置兜底）──


def _bing_html() -> str:
    return BING_FIXTURE.read_text(encoding="utf-8")


def test_parse_bing_extracts_title_url_snippet() -> None:
    hits = parse_bing(_bing_html(), limit=10)
    first = hits[0]
    assert first["url"] == "https://example.com/news/ai-chip-1"
    assert first["title"] == "国产 AI 芯片进展：新一代训练卡量产"
    assert "单卡算力较上代提升明显" in first["snippet"]


def test_parse_bing_ignores_non_result_blocks() -> None:
    # b_ans 之类的干扰块不含 b_algo，不能被当成结果
    urls = [hit["url"] for hit in parse_bing(_bing_html(), limit=10)]
    assert not any("bing.com/ignored" in url for url in urls)


def test_parse_bing_decodes_html_entities() -> None:
    # 摘要里混着 &ensp; / &#0183; / &amp;，必须解码成可读文本
    hits = parse_bing(_bing_html(), limit=10)
    first = hits[0]
    assert "&ensp;" not in first["snippet"] and "&#0183;" not in first["snippet"]
    assert "\u2003" not in first["snippet"] and "\u00b3" not in first["snippet"]
    assert "2026年8月19日" in first["snippet"]
    # 标题里的 <strong> 要剥掉
    assert hits[1]["title"] == "AI 芯片供应链最新动态"
    assert "&amp;" not in hits[1]["snippet"] and "&" in hits[1]["snippet"]


def test_parse_bing_unwraps_redirect_links() -> None:
    # Bing 有时把结果包成 /ck/a?...&u=a1<base64url>
    urls = [hit["url"] for hit in parse_bing(_bing_html(), limit=10)]
    assert "https://example.com/wrapped-article" in urls


def test_parse_bing_keeps_undecodable_redirect() -> None:
    # 没有 u 参数时原样保留，不能抛错、也不能丢结果
    urls = [hit["url"] for hit in parse_bing(_bing_html(), limit=10)]
    assert any("bing.com/ck/a?p=no-u-param" in url for url in urls)


def test_parse_bing_deduplicates_and_skips_non_http() -> None:
    urls = [hit["url"] for hit in parse_bing(_bing_html(), limit=10)]
    assert urls.count("https://example.com/news/ai-chip-1") == 1, "同一链接不应重复"
    assert not any(url.startswith("/") for url in urls), "相对链接跳过"


def test_parse_bing_falls_back_to_url_as_title() -> None:
    hits = parse_bing(_bing_html(), limit=10)
    fallback = [hit for hit in hits if hit["url"] == "https://example.com/news/no-title"]
    assert fallback and fallback[0]["title"] == "https://example.com/news/no-title"


def test_parse_bing_respects_limit() -> None:
    assert len(parse_bing(_bing_html(), limit=2)) == 2


@pytest.mark.parametrize("html", ["", "<html></html>", "完全不是 HTML", '<li class="b_algo"></li>'])
def test_parse_bing_survives_malformed_pages(html: str) -> None:
    # 页面改版或返回错误页时只能返回空列表，不能抛异常
    assert parse_bing(html, limit=5) == []


@respx.mock
async def test_bing_provider_sends_browser_headers_and_parses() -> None:
    respx.get(BING_ENDPOINT).mock(return_value=httpx.Response(200, text=_bing_html()))
    tool = _tool(provider="bing", search_results=3)
    result = await tool.run("AI 芯片")

    request = respx.calls[0].request
    # 默认 UA 会被搜索引擎拦截，必须带浏览器标识
    assert "Mozilla" in request.headers["user-agent"]
    assert request.url.params["q"] == "AI 芯片"
    assert len(result.sources) == 3
    assert "国产 AI 芯片进展" in result.text
    await tool.aclose()


@respx.mock
async def test_bing_empty_parse_is_reported_not_silently_empty() -> None:
    respx.get(BING_ENDPOINT).mock(return_value=httpx.Response(200, text="<html>no results</html>"))
    tool = _tool(provider="bing")
    result = await tool.run("AI")
    assert "没有搜索到相关结果" in result.text
    await tool.aclose()


@respx.mock
async def test_missing_tavily_key_returns_empty() -> None:
    tool = SearchTool(AgentSection(), Secrets(search_provider="tavily", tavily_api_key=""))
    result = await tool.run("AI")
    assert result.text == "没有搜索到相关结果。"
    await tool.aclose()


# ── 订阅源（SEARCH_PROVIDER=feeds）──


def _feed_items() -> list[dict[str, str]]:
    return parse_feed(FEED_FIXTURE.read_text(encoding="utf-8"), "example.com")


def test_parse_feed_extracts_fields_with_cdata() -> None:
    items = _feed_items()
    first = items[0]
    assert first["title"] == "超强厄尔尼诺已经形成"
    assert first["url"] == "https://www.solidot.org/story?sid=85569"
    assert "国家气候中心" in first["snippet"]
    assert first["published"].startswith("2026-10-09T06:59:11"), "UTC 归一后应是 06:59"
    assert first["source"] == "example.com"


def test_parse_feed_drops_boilerplate_and_promo() -> None:
    items = {item["title"]: item for item in _feed_items()}
    # InfoQ 那种 description 只有"点击查看原文>"，留空比留噪音好
    assert items["只有样板的摘要"]["snippet"] == ""
    # 爱范儿带公众号推广尾巴，摘要应保留正文、去掉推销
    promo = items["带推广尾巴的条目"]["snippet"]
    assert "MAKE 溜背 GREAT AGAIN" in promo
    assert "欢迎关注" not in promo and "微信号" not in promo


def test_parse_feed_strips_tracking_params() -> None:
    urls = [item["url"] for item in _feed_items()]
    assert "https://www.ifanr.com/1683999" in urls
    assert not any("utm_source" in url for url in urls), "跟踪参数应被去掉"


def test_parse_feed_skips_items_without_link() -> None:
    titles = [item["title"] for item in _feed_items()]
    assert "缺链接的条目" not in titles, "没有可抓取地址的条目必须跳过"


def test_parse_feed_keeps_item_with_unparsable_date() -> None:
    # 日期解析不了不该导致条目被丢，只是排序时落在最后
    items = {item["title"]: item for item in _feed_items()}
    assert items["坏日期的条目"]["published"] == ""


def test_parse_feed_handles_atom() -> None:
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<title>Atom 条目</title>"
        '<link rel="alternate" href="https://example.com/atom-1"/>'
        "<updated>2026-10-09T08:00:00Z</updated>"
        "<summary>Atom 摘要内容，长度足够通过清洗阈值。</summary>"
        "</entry></feed>"
    )
    items = parse_feed(atom, "atom.example")
    assert items[0]["url"] == "https://example.com/atom-1"
    assert items[0]["title"] == "Atom 条目"
    assert items[0]["published"].startswith("2026-10-09T08:00:00")


@pytest.mark.parametrize("text", ["", "<rss></rss>", "不是 XML", "<rss><channel><item></item></channel></rss>"])
def test_parse_feed_survives_malformed_input(text: str) -> None:
    assert parse_feed(text, "x") == []


def test_match_feed_items_prefers_more_keyword_hits() -> None:
    # 只判断"命中任一词"会让「AI 芯片」退化成长度匹配"AI"，得分排序才对
    items = [
        {"title": "AI 芯片新进展", "url": "https://a/1", "snippet": "", "published": "2026-10-09T01:00:00+00:00"},
        {"title": "AI 工具推荐", "url": "https://a/2", "snippet": "", "published": "2026-10-09T09:00:00+00:00"},
    ]
    hits, note = match_feed_items(items, "AI 芯片", limit=5)
    assert hits[0]["url"] == "https://a/1", "两个词都命中的应排在前"
    assert note == ""


def test_match_feed_items_reports_partial_match() -> None:
    items = [{"title": "AI 工具推荐", "url": "https://a/2", "snippet": "", "published": ""}]
    hits, note = match_feed_items(items, "AI 芯片", limit=5)
    assert len(hits) == 1
    # 必须如实说明只匹配了部分关键词，否则用户会以为结果都关于芯片
    assert "芯片" in note
    assert "只匹配到其余关键词" in note


def test_match_feed_items_reports_no_match_but_returns_latest() -> None:
    items = [
        {"title": "无关内容", "url": "https://a/1", "snippet": "", "published": "2026-10-09T01:00:00+00:00"},
        {"title": "也是无关", "url": "https://a/2", "snippet": "", "published": "2026-10-09T09:00:00+00:00"},
    ]
    hits, note = match_feed_items(items, "量子计算", limit=5)
    assert [hit["url"] for hit in hits] == ["https://a/2", "https://a/1"], "未命中时给最新条目"
    assert "没有匹配该关键词" in note


def test_match_feed_items_ignores_function_words_in_whole_sentence() -> None:
    # 实测：Agent 把整句问话当关键词传进来，功能词参与匹配导致
    # 「没有同时包含『新闻、今天、重要』的条目」这种误导性提示
    items = [
        {
            "title": "具身智能公司完成融资",
            "url": "https://a/1",
            "snippet": "",
            "published": "2026-10-09T09:00:00+00:00",
        },
        {"title": "无关内容", "url": "https://a/2", "snippet": "", "published": "2026-10-09T08:00:00+00:00"},
    ]
    hits, note = match_feed_items(items, "今天有哪些重要的具身智能新闻？", limit=5)
    assert [hit["url"] for hit in hits] == ["https://a/1"]
    assert note == "", "功能词不该被当成缺失的关键词报出来"


def test_match_feed_items_still_reports_real_missing_keyword() -> None:
    # 剔除功能词不能把"真的少了一个实词"这件事一起吞掉
    items = [{"title": "AI 与具身智能公司完成融资", "url": "https://a/1", "snippet": "", "published": ""}]
    _, note = match_feed_items(items, "今天有哪些 AI 芯片新闻？", limit=5)
    assert "芯片" in note


def test_match_feed_items_falls_back_when_only_function_words() -> None:
    # 整句都是功能词时不能返回空关键词表，否则退化成"返回最新内容"还带一遍无关提示
    items = [{"title": "有哪些新闻", "url": "https://a/1", "snippet": "", "published": ""}]
    hits, note = match_feed_items(items, "有哪些新闻", limit=5)
    assert [hit["url"] for hit in hits] == ["https://a/1"]
    assert note == ""


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("今天有哪些重要的人工智能新闻？请给出 3 条要点", ["人工智能"]),
        ("AI 芯片", ["AI", "芯片"]),
        ("量子计算", ["量子计算"]),
        # 整句只剩一个内容词时也要能剥干净，否则整句进匹配永远不命中
        ("具身智能机器人产业最新进展如何", ["具身智能机器人产业", "进展"]),
    ],
)
def test_phrases_keep_only_content_words(query: str, expected: list[str]) -> None:
    assert _phrases(query) == expected


def test_keywords_expand_long_words_for_matching() -> None:
    # 没有分词器时整串长词不可能出现在标题里，切窗后才有机会命中
    words = _keywords("具身智能机器人产业")
    assert "具身智能机器人产业" in words
    assert "机器人" in words
    assert all(len(word) >= 2 for word in words)


def test_long_word_windows_do_not_leak_into_note() -> None:
    items = [{"title": "某公司发布机器人新品", "url": "https://a/1", "snippet": "", "published": ""}]
    query = "具身智能机器人产业 融资"
    hits, note = match_feed_items(items, query, limit=5)
    assert [hit["url"] for hit in hits] == ["https://a/1"], "长词切窗应让它能命中"
    # 提示语只能出现内容词，不能出现「能机器」这类切出来的窗口片段
    quoted = note.split("「")[1].split("」")[0].split("、")
    assert all(word in _phrases(query) for word in quoted)


def test_ascii_keywords_require_word_boundary() -> None:
    # 英文源里几乎每篇都含 said/email/chain，子串匹配会把「AI」变成万能匹配
    items = [
        {"title": "He said the email chain failed", "url": "https://a/1", "snippet": "", "published": ""},
        {"title": "OpenAI ships a new model", "url": "https://a/2", "snippet": "", "published": ""},
        {"title": "AI 芯片新进展", "url": "https://a/3", "snippet": "", "published": ""},
    ]
    hits, note = match_feed_items(items, "AI", limit=5)
    assert [hit["url"] for hit in hits] == ["https://a/3"], "只有真正独立的 AI 才算命中"
    assert note == ""


def test_match_feed_items_deduplicates_and_limits() -> None:
    items = [
        {"title": "同名文章", "url": "https://a/1", "snippet": "", "published": "2026-10-09T09:00:00+00:00"},
        {
            "title": "同名文章（另一源转载）",
            "url": "https://a/1",
            "snippet": "",
            "published": "2026-10-09T08:00:00+00:00",
        },
        {"title": "第二篇", "url": "https://a/2", "snippet": "", "published": "2026-10-09T07:00:00+00:00"},
    ]
    hits, _ = match_feed_items(items, "文章", limit=1)
    assert len(hits) == 1
    # 有匹配项时不再掺入未匹配项（这是搜索语义，不是"最新列表"）
    hits2, _ = match_feed_items(items, "文章", limit=5)
    assert [hit["url"] for hit in hits2] == ["https://a/1"]


@pytest.mark.parametrize(
    ("raw", "expected_empty"),
    [
        ("点击查看原文>", True),
        ("阅读原文", True),
        ("太短", True),
        ("刚好十二个字符的摘要内容", False),
        ("只有十一个字符", True),
    ],
)
def test_clean_snippet_thresholds(raw: str, expected_empty: bool) -> None:
    assert (clean_snippet(raw) == "") is expected_empty


@respx.mock
async def test_feeds_provider_does_not_wait_for_slow_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    # hnrss 实测会挂 20～36 秒，而检索必须等所有源返回；单源超时必须让它被放弃
    good = "https://good.example/feed"
    slow = "https://slow.example/feed"
    respx.get(good).mock(return_value=httpx.Response(200, text=FEED_FIXTURE.read_text(encoding="utf-8")))

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="")

    respx.get(slow).mock(side_effect=hang)
    tool = SearchTool(
        AgentSection(search_results=3),
        Secrets(search_provider="feeds"),
        feeds=[good, slow],
        feed_timeout=0.05,
    )
    started = time.monotonic()
    result = await tool.run("厄尔尼诺")
    elapsed = time.monotonic() - started
    assert elapsed < 1, f"不该等慢源，实际耗时 {elapsed:.2f}s"
    assert result.sources, "好源的结果必须保留"
    await tool.aclose()


@respx.mock
async def test_feeds_provider_without_feeds_is_reported() -> None:
    tool = SearchTool(AgentSection(), Secrets(search_provider="feeds"), feeds=[])
    result = await tool.run("AI")
    assert "没有搜索到相关结果" in result.text
    await tool.aclose()
