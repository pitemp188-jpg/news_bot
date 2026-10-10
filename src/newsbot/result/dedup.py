"""
模块: result.dedup
职责: 内容去重——同一件事的多家报道只留质量最高的一条（来源不同不算重复）
依赖: core.db, core.models
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import NewsItem

# 追踪参数不带信息，参与比对会造成同一页面被判为不同
_TRACKING_PREFIXES = ("utm_", "spm", "from", "share_", "ref", "fbclid", "gclid")
_SIMHASH_BITS = 64
_SEGMENT_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
# 带数字的标识符：模型名 / 版本号 / 产品编号（gpt-5、qwen-image-2.1-turbo、12b）
_TERM_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*[0-9][a-z0-9._+-]*")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# 近逐字重复（转载）的阈值。实测真实订阅源里**不同话题**的词集相似度上限只有
# 0.17（80 条两两比对），所以 0.6 只会命中几乎一字不差的转载，不会误伤同话题的
# 不同报道——这是把阈值定在 0.6 而非 0.7～0.8 的实测依据。
_PARAPHRASE_SIMILARITY = 0.6
# 词少于这么多个就不做词集判断：样本太小，相似度全是噪声
_MIN_TOKENS = 12


def normalize_url(url: str) -> str:
    """去掉片段、追踪参数与末尾斜杠，统一主机名大小写。"""
    parts = urlsplit(url.strip())
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(key)]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return any(lowered.startswith(prefix) for prefix in _TRACKING_PREFIXES)


def url_digest(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def content_digest(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    """拉丁词按词切分，中文按二元组切分——二元组对同题改写的容忍度更好。"""
    tokens: list[str] = []
    for segment in _SEGMENT_RE.findall((text or "").lower()):
        if segment.isascii() or len(segment) == 1:
            tokens.append(segment)
        else:
            tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return tokens


def simhash64(text: str) -> int:
    """按词频加权的 64 位 simhash；文本为空时返回 0。"""
    tokens = _tokens(text)
    if not tokens:
        return 0
    weights: dict[str, int] = {}
    for token in tokens:
        weights[token] = weights.get(token, 0) + 1
    vector = [0] * _SIMHASH_BITS
    for token, weight in weights.items():
        digest = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(_SIMHASH_BITS):
            vector[bit] += weight if digest >> bit & 1 else -weight
    result = 0
    for bit, score in enumerate(vector):
        if score > 0:
            result |= 1 << bit
    return result


def hamming(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def identifiers(text: str) -> set[str]:
    """取出带数字的标识符：模型名、版本号、产品编号。

    这是**唯一能跨语言**对齐"同一件事"的词法证据：中文标题与英文标题用词几乎不
    重合（实测「阿里发布 Qwen-Image-2.1-Turbo」与「Alibaba Qwen Releases
    Qwen-Image-2.1-Turbo」共享的普通词为零），但模型名、版本号是同一个字符串。
    纯年份与一两位数字不指向具体事件，必须排除——实测「iPhone 18」与
    「TechCrunch Disrupt 2026」这类会把毫不相干的文章拉到一起。
    """
    found = _TERM_RE.findall((text or "").lower())
    return {token for token in found if not _YEAR_RE.match(token) and not (token.isdigit() and len(token) < 3)}


def same_story(left: str, right: str) -> bool:
    """两段内容是否在讲同一件事。

    只认强证据，**宁可漏判也不误删**：误删会让读者永远看不到那条来源，漏判只是
    多一条链接（正文层面的同事件合并交给 Agent，见 agent/prompts.py）。实测被
    否掉的判据：用「共享若干普通拉丁词」判重会误删——「Alibaba Qwen Releases X」
    与「JetBrains Releases Y」共享 releases/model，以及同活动的两篇不同报道会
    共享活动名，都会把**不同的事**并成一件。
    """
    if not left or not right:
        return False
    if content_digest(left) == content_digest(right):
        return True
    # 共同的标识符（qwen-image-2.1-turbo / cgroup-v2）几乎不可能是巧合
    if identifiers(left) & identifiers(right):
        return True
    left_tokens, right_tokens = set(_tokens(left)), set(_tokens(right))
    if len(left_tokens) < _MIN_TOKENS or len(right_tokens) < _MIN_TOKENS:
        return False
    return jaccard(left_tokens, right_tokens) >= _PARAPHRASE_SIMILARITY


def jaccard(left: set[str], right: set[str]) -> float:
    """词集相似度；任一侧为空视为不相似。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class Item:
    """待比对的资讯条目。

    `text` 与 `weight` 是内容去重的前提：只拿标题判不出"同一件事"（各家标题措辞
    差异极大），而同一件事的多家报道里保留哪一条，取决于信息量与来源权威度。
    """

    title: str
    url: str
    text: str = ""
    weight: float = 1.0

    @property
    def body(self) -> str:
        """参与判重的内容：标题 + 摘要/正文。

        两者都要：实测 InfoQ 与量子位的多数条目没有摘要，只有摘要就无从比对；
        而只看标题又不足以判断"同一件事"。
        """
        return f"{self.title} {self.text}".strip()

    @property
    def richness(self) -> int:
        """信息量。同一件事的多家报道里，能支撑结论的是信息更全的那条。"""
        return len(self.body)


@dataclass
class Deduper:
    """内容去重：同一 URL、逐字转载、同一件事只留一条。

    与旧实现的关键差别：判据从"URL 与近 N 天指纹"变成**内容**，並且**保留质量
    最高**的那条而不是碰巧排在最前的那条。同一家媒体的两篇不同报道必须都保留——
    按来源去重会把一个媒体一天的多条新闻压成一条，那不是去重，是丢失。
    """

    max_distance: int = 3
    min_similarity: float = _PARAPHRASE_SIMILARITY
    min_tokens: int = _MIN_TOKENS
    urls: set[str] = field(default_factory=set)
    digests: set[str] = field(default_factory=set)
    simhashes: list[int] = field(default_factory=list)
    token_sets: list[set[str]] = field(default_factory=list)
    # 标识符 → 已见条目的内容，用于 O(1) 判断"这件事见过没有"
    terms: dict[str, list[str]] = field(default_factory=dict)

    def remember(self, item: Item) -> None:
        self.urls.add(url_digest(item.url))
        body = item.body
        for candidate in _bodies(item):
            self.digests.add(content_digest(candidate))
        signature = simhash64(body)
        if signature:
            self.simhashes.append(signature)
        tokens = set(_tokens(body))
        if tokens:
            self.token_sets.append(tokens)
        for token in identifiers(body):
            self.terms.setdefault(token, []).append(body)

    def is_duplicate(self, item: Item) -> bool:
        """与已记录内容比对（含 `load_recent` 载入的历史）。

        这是 `same_story` 判据的**索引化等价实现**：历史可能有几千条，逐条算
        相似度会把一次去重拖成秒级，所以网址、内容指纹、标识符都用集合/索引查，
        只有词集相似度需要逐个比对（发现第一个即返回）。
        """
        if url_digest(item.url) in self.urls:
            return True
        body = item.body
        if any(content_digest(candidate) in self.digests for candidate in _bodies(item)):
            return True
        fresh = identifiers(body)
        if any(known in self.terms for known in fresh):
            return True
        tokens = set(_tokens(body))
        if len(tokens) < self.min_tokens:
            return False
        if any(
            len(known) >= self.min_tokens and jaccard(tokens, known) >= self.min_similarity for known in self.token_sets
        ):
            return True
        signature = simhash64(body)
        return bool(signature) and any(hamming(signature, known) <= self.max_distance for known in self.simhashes)

    def keep_indexes(self, items: list[Item]) -> list[int]:
        """返回应当保留的下标（升序）；重复项的下标不在其中。

        判定顺序按质量从高到低：质量高的先"占位"，之后与它重复的才被判为重复。
        同一件事的多家报道因此留下的是信息最全、来源最权威的那条，输出顺序仍是
        传入顺序（下游的编号与引用顺序都不受影响）。
        """
        order = sorted(range(len(items)), key=lambda index: self._quality(items[index]), reverse=True)
        kept: list[int] = []
        for index in order:
            item = items[index]
            # 与历史指纹比对走索引（历史可能有几千条，逐条算相似度太慢），
            # 与本次已占位的条目比对用完整内容判据
            if self.is_duplicate(item) or any(self._same_as_kept(item, items[other]) for other in kept):
                continue
            kept.append(index)
        kept.sort()
        for index in kept:
            self.remember(items[index])
        return kept

    @staticmethod
    def _same_as_kept(item: Item, other: Item) -> bool:
        """与本次已保留的条目比对：同一网址，或同一件事。"""
        if url_digest(item.url) == url_digest(other.url):
            return True
        return same_story(item.body, other.body)

    @staticmethod
    def _quality(item: Item) -> tuple[int, float]:
        """同一件事保留哪一条：先比信息量，并列时比来源权威度。"""
        return (item.richness, item.weight)

    def filter(self, items: list[Item]) -> list[Item]:
        """返回未重复的条目（保持传入顺序）。"""
        return [items[index] for index in self.keep_indexes(items)]

    async def load_recent(self, db: Database, days: int) -> int:
        """载入近 N 天已入库资讯的指纹，避免与历史推送重复。

        历史只存了标题（没有摘要），所以历史侧的标识符只能从标题里取——这不影响
        跨语言判重：模型名与版本号在标题里就会出现。
        """
        since = datetime.now(UTC) - timedelta(days=max(1, days))
        async with db.session() as session:
            rows = (await session.execute(select(NewsItem).where(NewsItem.fetched_at >= since))).scalars().all()
        for row in rows:
            self.urls.add(row.url_hash or url_digest(row.url))
            if row.content_hash:
                self.digests.add(row.content_hash)
            if row.simhash:
                self.simhashes.append(int(row.simhash, 16))
            title = row.title or ""
            for token in identifiers(title):
                self.terms.setdefault(token, []).append(title)
        return len(rows)


def simhash_hex(signature: int) -> str:
    """simhash 转 16 位十六进制，便于存库。"""
    return f"{signature:016x}"


def _bodies(item: Item) -> set[str]:
    """参与指纹的内容形态：标题 + 摘要，以及单独的摘要。

    单独的摘要也记一份，是因为"正文一字不差、只有标题被改写的转载"很常见；
    标题 + 摘要一起记，是因为多数条目没有摘要（实测 InfoQ / 量子位如此），
    而模型名与版本号往往只在标题里。
    """
    bodies = {item.body}
    text = item.text.strip()
    if text:
        bodies.add(text)
    return bodies
