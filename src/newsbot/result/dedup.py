"""
模块: result.dedup
职责: 内容去重——同一件事的多家报道只留质量最高的一条（来源不同不算重复）
依赖: core.db, core.llm, core.log, core.models
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.llm import LLM, Usage
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem

logger = get_logger(__name__)

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
# 模型输出里"整行都是编号"的行才当分组；说明文字、代码块标记、表头一律忽略
_GROUP_LINE = re.compile(r"^\s*[-*•]?\s*(\d+(?:\s*[,，、]\s*\d+)+)\s*$")
_GROUP_SPLIT = re.compile(r"[,，、]")
# 语义分组的提示词。任务只有一个：判断"这几条说的是不是同一件事"。
# 保留取舍不交给模型（见 StoryGrouper），所以这里只需要它给分组。
# 格式要求写在末尾——实测放在前面时模型会先写一大段分析再给结论。
_GROUP_PROMPT = """下面是一批资讯，每条带编号。请找出其中**讲的是同一件事**的条目。

判断标准：
- 同一件事 = 同一次发布、同一笔融资、同一起事故、同一项政策等；多家媒体各自报道算同一件事。
- 标题措辞不同（含中英文互译、简繁体）但说的是同一件事的，算同一件事。
- **同一家媒体的两条不同新闻不算**；同一主题下的不同事件（例如两家公司各自发布模型）不算。
- 拿不准就不要归组。
"""
_GROUP_TAIL = """
现在按上面的标准给出分组：每行一组，只写编号，用英文逗号分隔（例如 `3,7,12`）；没有任何两条是同一件事就只输出 `无`。
**不要输出任何解释、标题、表头或其他文字。**
"""
# 机械型任务的输出上限。分组结果通常只有几个字符（"3,7"），但**当前的模型是推理
# 模型**，同一件事它会先烧掉两千多个推理 token 才给结论（实测 2259～2588），而且
# 推理与正文共用这个额度——上限 1000 时推理吃光额度、正文为空，调用"成功"却拿不到
# 任何结果。4000 是实测能稳定拿到正文的值。
_GROUP_MAX_TOKENS = 4000


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
        """信息量。同一件事的多家报道里，能支撑结论的是信息更全的那条。

        用**词数**而不是字符数：同样信息量下英文字符数天然多于中文，按字符数比
        （实测）会让英文源系统性地顶掉信息同样完整的中文源。`_tokens` 对拉丁词
        按词切、对中文按二元组切，是同一把尺子。
        """
        return len(_tokens(self.body))


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


# 摘要型条目（早报 / 日报 / 盘点…）是**容器**而不是"一件事"：它一次装好几条新闻。
# 实测（ifanr 的「早报｜苹果定档…/三星减产…/保时捷裁员 9000 个岗位」）它因为正文里
# 提到 Manus 融资，被判定与「Manus 成功融资逾 5 亿美元」是同一件事而合并掉，
# 结果另外四条新闻跟着消失。所以这类条目不参与内容合并。
_DIGEST_RE = re.compile(
    r"早报|晚报|日报|周报|简报|快讯|盘点|汇总|一览|要闻|周末版|roundup|digest|weekly|newsletter",
    re.I,
)


def is_digest(item: Item) -> bool:
    """是否是摘要型条目（一次装多条新闻）。"""
    return _DIGEST_RE.search(item.title or "") is not None


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
    # 可选的语义分组器；不装就只用词法判据（零模型成本）
    grouper: StoryGrouper | None = None
    # 去重过程中产生的模型用量（语义分组），由流水线写进 llm_usage
    usages: list[Usage] = field(default_factory=list)

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
        """与历史指纹比对（含 `load_recent` 载入的历史）。

        这是 `same_story` 判据的**索引化等价实现**：历史可能有几千条，逐条算
        相似度会把一次去重拖成秒级，所以网址、内容指纹、标识符都用集合/索引查，
        只有词集相似度需要逐个比对（发现第一个即返回）。

        摘要型条目只按"网址或正文完全相同"判重：历史里恰好有一条与它提到的某条
        新闻相同，不代表整份早报已经推送过。
        """
        if url_digest(item.url) in self.urls:
            return True
        body = item.body
        if any(content_digest(candidate) in self.digests for candidate in _bodies(item)):
            return True
        if is_digest(item):
            return False
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

    async def keep(self, items: list[Item]) -> list[int]:
        """先做语义分组（装了分组器时），再按内容判据决定保留哪些下标。

        词法判据与语义判据是**叠加**而不是二选一：词法能可靠抓到的（同 URL、
        逐字转载、带版本号的标识符）不依赖模型；模型只补上词法的盲区。

        摘要型条目（早报/盘点）不参与语义分组——它是容器，一次装多条新闻，把它
        跟其中一条合并会连带丢掉其余几条。它仍会参与词法判据，但只按"网址或正文
        完全相同"判重（见 `_same_as_kept` / `is_duplicate`）。
        """
        grouping = Grouping()
        if self.grouper is not None:
            candidates = [index for index, item in enumerate(items) if not is_digest(item)]
            local = await self.grouper.group([items[index] for index in candidates])
            grouping = Grouping(
                # 把"候选列表里的位置"还原成"传入列表里的下标"
                groups={candidates[position]: tag for position, tag in local.groups.items()},
                usage=local.usage,
            )
        kept = self.keep_indexes(items, groups=grouping.groups)
        if grouping.usage is not None:
            self.usages.append(grouping.usage)
        return kept

    def keep_indexes(self, items: list[Item], groups: dict[int, int] | None = None) -> list[int]:
        """返回应当保留的下标（升序）；重复项的下标不在其中。

        判定顺序按质量从高到低：质量高的先"占位"，之后与它重复的才被判为重复。
        同一件事的多家报道因此留下的是信息最全、来源最权威的那条，输出顺序仍是
        传入顺序（下游的编号与引用顺序都不受影响）。

        `groups` 是语义分组给出的"下标 → 组号"（见 `StoryGrouper`）：同组视为
        同一件事。它只影响"要不要合并"，不影响"保留哪一条"。
        """
        order = sorted(range(len(items)), key=lambda index: self._quality(items[index]), reverse=True)
        kept: list[int] = []
        for index in order:
            item = items[index]
            # 与历史指纹比对走索引（历史可能有几千条，逐条算相似度太慢），
            # 与本次已占位的条目比对用完整内容判据
            if self.is_duplicate(item) or any(
                self._same_as_kept(item, items[other]) or same_group(index, other, groups) for other in kept
            ):
                continue
            kept.append(index)
        kept.sort()
        for index in kept:
            self.remember(items[index])
        return kept

    @staticmethod
    def _same_as_kept(item: Item, other: Item) -> bool:
        """与本次已保留的条目比对：同一网址，或同一件事。

        摘要型条目与普通条目之间不做内容判等：早报里提到某条新闻不等于"它就是
        那条新闻"，合并会连带丢掉早报里的其他几条（实测踩到）。
        """
        if url_digest(item.url) == url_digest(other.url):
            return True
        if is_digest(item) != is_digest(other):
            return False
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


@dataclass
class Grouping:
    """一次语义分组的结果。

    `groups` 是"下标 → 组号"，只包含组内**至少两条**的成员；不需要分组的条目
    不出现在里面（等价于"它是独立的一件事"）。
    """

    groups: dict[int, int] = field(default_factory=dict)
    usage: Usage | None = None


def parse_groups(text: str, total: int) -> dict[int, int]:
    """把模型输出解析成"0 基下标 → 组号"。

    容错优先，因为模型输出不可控：只认"整行都是编号"的行（其余行一律忽略），
    越界编号丢掉，少于两个有效编号的行忽略；同一个编号出现在多组时**把这几组
    并成一组**——丢哪一组都可能让本该合并的来源漏出去，取并集最多是多合并，
    方向上是安全的。
    """
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        while parent.get(node, node) != node:
            parent[node] = parent.get(parent[node], parent[node])
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for line in (text or "").splitlines():
        match = _GROUP_LINE.match(line)
        if match is None:
            continue
        # 模型给的是 1 基编号，转成 0 基；只保留落在范围内的
        members = sorted({int(part) - 1 for part in _GROUP_SPLIT.split(match.group(1)) if 1 <= int(part) <= total})
        if len(members) < 2:
            continue
        for member in members[1:]:
            union(members[0], member)

    grouped: dict[int, list[int]] = {}
    # 遍历"出现过的所有节点"：并查集只把被 union 的一方写进 parent，
    # 每组的代表节点本身不在里面，只遍历 parent 会把代表漏掉（实测因此少合并一条）
    nodes = set(parent) | set(parent.values())
    for node in nodes:
        grouped.setdefault(find(node), []).append(node)
    groups: dict[int, int] = {}
    number = 0
    for members in grouped.values():
        if len(members) < 2:
            continue
        for member in members:
            groups[member] = number
        number += 1
    return groups


def same_group(left: int, right: int, groups: dict[int, int] | None) -> bool:
    """两个下标是否被语义分组划到同一组。"""
    if not groups:
        return False
    tag = groups.get(left)
    return tag is not None and tag == groups.get(right)


@dataclass
class StoryGrouper:
    """用一次 LLM 调用把讲同一件事的条目分组。

    只让模型做"这几条是不是同一件事"这个判断——这是词法判据的盲区（中文改写、
    没有版本号的纯中文事件）。**保留哪一条仍由确定性规则决定**（信息量优先、
    来源权重次之，见 `Deduper._quality`），模型不参与取舍，因此它不可能把来源
    弄丢、也不可能让小道消息顶掉权威源。

    语义分组是**增强而不是必要条件**：条目太少不值得调用、超时、异常、输出解析
    不出来，一律返回空分组并退回词法判据。宁可漏合并，也不能因为模型抽风丢掉来源。
    """

    llm: LLM
    timeout: float = 60.0
    min_items: int = 4
    max_items: int = 40
    max_tokens: int = _GROUP_MAX_TOKENS
    sample_chars: int = 60

    def build_prompt(self, items: list[Item]) -> str:
        """拼提示词：条目列表夹在中间，格式要求放在最末尾。

        实测：格式要求写在前面时模型倾向先写一段分析再给结论（2588 个 completion
        token），放到末尾明显收敛。
        """
        listing = "\n".join(
            f"{index}. {item.title[: self.sample_chars]}"
            + (f" | {item.text[: self.sample_chars]}" if item.text.strip() else "")
            for index, item in enumerate(items, 1)
        )
        return f"{_GROUP_PROMPT}\n{listing}\n{_GROUP_TAIL}"

    async def group(self, items: list[Item]) -> Grouping:
        if not self.min_items <= len(items) <= self.max_items:
            return Grouping()
        messages = [{"role": "user", "content": self.build_prompt(items)}]
        try:
            reply = await asyncio.wait_for(self.llm.complete(messages, max_tokens=self.max_tokens), self.timeout)
        except Exception as exc:  # 任何失败都只是"这次没有语义分组"
            logger.warning("语义分组失败，退回词法判据: %s: %s", type(exc).__name__, exc)
            return Grouping()
        groups = parse_groups(reply.content, len(items))
        if groups:
            logger.info("语义分组：%d 条来源被划入 %d 组同一件事", len(groups), len(set(groups.values())))
        elif not (reply.content or "").strip():
            # 正文为空而调用成功：多半是推理 token 把额度吃光（推理与正文共用额度），
            # 这时调大 dedup 的输出上限才有用——留一条可操作的日志，别让人以为"模型说没重复"
            logger.warning("语义分组没有返回正文（可能被判定的条目过多或输出上限过小），已退回词法判据")
        else:
            # 解析不出分组但模型确实回了东西——留证据，便于判断是不是提示词被忽略了
            logger.info("语义分组未识别出分组，模型输出: %s", reply.content.strip()[:120])
        used = reply.usage() if (reply.prompt_tokens or reply.completion_tokens) else None
        return Grouping(groups=groups, usage=used)
