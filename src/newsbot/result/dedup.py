"""
模块: result.dedup
职责: 去重——URL 归一化、内容指纹、simhash 近似去重、与近 N 天已推送内容比对
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


def jaccard(left: set[str], right: set[str]) -> float:
    """词集相似度；任一侧为空视为不相似。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class Item:
    """待比对的资讯条目。"""

    title: str
    url: str
    text: str = ""


@dataclass
class Deduper:
    """URL 与近似内容双重去重；可先从库中载入近期指纹。"""

    max_distance: int = 3
    min_similarity: float = 0.7
    min_tokens: int = 5
    urls: set[str] = field(default_factory=set)
    digests: set[str] = field(default_factory=set)
    simhashes: list[int] = field(default_factory=list)
    token_sets: list[set[str]] = field(default_factory=list)

    def remember(self, item: Item) -> None:
        self.urls.add(url_digest(item.url))
        body = item.text or item.title
        self.digests.add(content_digest(body))
        signature = simhash64(body)
        if signature:
            self.simhashes.append(signature)
        tokens = set(_tokens(body))
        if tokens:
            self.token_sets.append(tokens)

    def is_duplicate(self, item: Item) -> bool:
        if url_digest(item.url) in self.urls:
            return True
        body = item.text or item.title
        if content_digest(body) in self.digests:
            return True
        # 短文本的 simhash 不稳，词集相似度更可靠；词太少时不做近似判断
        tokens = set(_tokens(body))
        if len(tokens) >= self.min_tokens and any(
            len(known) >= self.min_tokens and jaccard(tokens, known) >= self.min_similarity for known in self.token_sets
        ):
            return True
        signature = simhash64(body)
        return bool(signature) and any(hamming(signature, known) <= self.max_distance for known in self.simhashes)

    def filter(self, items: list[Item]) -> list[Item]:
        """返回未重复的条目，并把它们记入指纹集合。"""
        kept: list[Item] = []
        for item in items:
            if self.is_duplicate(item):
                continue
            self.remember(item)
            kept.append(item)
        return kept

    async def load_recent(self, db: Database, days: int) -> int:
        """载入近 N 天已入库资讯的指纹，避免与历史推送重复。"""
        since = datetime.now(UTC) - timedelta(days=max(1, days))
        async with db.session() as session:
            rows = (await session.execute(select(NewsItem).where(NewsItem.fetched_at >= since))).scalars().all()
        for row in rows:
            self.urls.add(row.url_hash or url_digest(row.url))
            if row.content_hash:
                self.digests.add(row.content_hash)
            if row.simhash:
                self.simhashes.append(int(row.simhash, 16))
        return len(rows)


def simhash_hex(signature: int) -> str:
    """simhash 转 16 位十六进制，便于存库。"""
    return f"{signature:016x}"
