"""
模块: result.cluster
职责: 事件聚簇——把最近两周的报道归到同一件事上，写 story 与 report_item
依赖: core.config, core.db, core.llm, core.log, core.models, result.dedup
来源: 两段式归组（模型判定 + 复核）与 role 语义移植自 AIHOT（MIT，KKKKhazix/AIHOT）
      packages/backend/src/events/cluster.ts，按本项目的数据模型与既有 LLM 客户端重写
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.llm import LLM, Usage
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem, ReportItem, Story
from newsbot.result.dedup import _tokens, identifiers, jaccard

logger = get_logger(__name__)

# 关系只有三种。"后续进展"必须与"重复报道"分开：前者要为事件产生**新的热度**，
# 后者只是多一家来源印证同一件事（`result/hot.py` 的计数就按这个分）
REL_SAME = "same"
REL_FOLLOW_UP = "follow_up"
REL_DIFFERENT = "different"
RELATIONS = (REL_SAME, REL_FOLLOW_UP, REL_DIFFERENT)

ROLE_PRIMARY = "primary"
ROLE_FOLLOW_UP = "follow_up"
ROLE_DUPLICATE = "duplicate"

# 模型输出：`左编号,右编号,关系`。只认符合这个形状的行，其余行忽略。
# 容错要点（实测模型会很自然地把结论写成 `- 1,2,same` 或 `` `1,2,same` ``）：
# 允许行首的项目符号与反引号，允许行尾的反引号与句号
_REL_LINE = re.compile(r"^\s*[-*•`]*\s*(\d+)\s*[,，\s]\s*(\d+)\s*[,，\s]\s*([a-z_]+)\s*[`.]*\s*$", re.MULTILINE)
# 复核阶段的"否"：整行是一个 `no` / `不同`
_VETO_LINE = re.compile(r"^\s*(?:no|不同|否|不是)\s*$", re.MULTILINE | re.IGNORECASE)

_PAIR_PROMPT = """下面是一批资讯的两两配对，每条带编号。请判断每一对的关系。

关系只有三种：
- `same`：同一件事的重复报道（同一次发布、同一笔融资、同一起事故被多家报道，含中英文互译）
- `follow_up`：同一件事的后续进展（同一件事有了新信息：回应、数据更新、下一步动作）
- `different`：两件不同的事

判断要点：
- **同一主题不算同一件事**（两家公司各自发布模型、两个城市的同类政策各算不同的两件事）。
- 标题措辞不同但事实相同的，算 `same`。
- 后续进展必须是**同一件事**的新信息；只是换个人再说一遍事实的，算 `same` 而不是 `follow_up`。
"""
_PAIR_TAIL = """
现在逐行给出结论，每行一条：`左编号,右编号,关系`。
例如 `1,3,same`。**不要输出表头、解释或其他任何文字**；拿不准的按 `different` 输出。
"""

_REVIEW_PROMPT = """下面是若干组资讯，每组被自动归到了"同一件事"。请逐组复核。

每组只要有一条明显不属于同一件事，就判定这一组**不成立**。
同一主题下的不同事件（两家公司各自的发布、两个城市各自的政策）属于**不成立**。
"""
_REVIEW_TAIL = """
逐行输出复核结论：组号与结论用逗号分隔，`same` 表示这组确实是一件事，`no` 表示不成立。
例如 `2,no`。只复核你认为**不成立**的组即可，成立的不用输出。
**不要输出表头、解释或其他任何文字。**
"""

# 聚簇是机械型任务，但当前模型是推理模型：推理 token 与正文共用输出额度，
# 上限给小了会出现"调用成功但正文为空"（实测 1000 时正文为空）
_CLUSTER_MAX_TOKENS = 16000
# "不确定就合并"要求的最低结论覆盖面。低于它就说明这些配对根本没问到模型，
# 不合并（宁可漏合并，也不能凭空把几十条资讯并成一件事）
MIN_COVERAGE = 0.5
# 预筛认为"有识别力"的标识符最小长度。实测 `2.1`、`1000` 这类短碎片会把两件毫不
# 相干的事拉到一起（「Mellum2.1」对「Nano Banana 2.1」），详见 `_specific_identifiers`
MIN_IDENTIFIER_CHARS = 8


@dataclass
class Candidate:
    """参与聚簇的一条报道。"""

    news_item_id: int
    title: str
    text: str = ""
    source: str = ""
    url: str = ""
    published_at: datetime | None = None
    tier: str = "T2"
    editorial: bool = True

    @property
    def body(self) -> str:
        return f"{self.title} {self.text}".strip()

    @property
    def participant(self) -> str:
        """这条报道代表哪个独立主体。缺来源时退回 host，最后退回 id。

        **绝不能留空**：空参与者会被热度当成同一个未知主体互相抵消，
        多家来源报道同一件事反而算不出热度。空值在这里退化成 `unknown:{id}`，
        至少保证"两条空来源的报道不会被当成同一家媒体"。
        """
        name = (self.source or _host(self.url)).strip()
        return name or f"unknown:{self.news_item_id}"


@dataclass
class Pair:
    """一对关系判定。`certain=False` 表示模型没给出可用结论。"""

    left: int
    right: int
    relation: str = REL_DIFFERENT
    certain: bool = False


@dataclass
class Cluster:
    """一组属于同一件事的报道。"""

    members: list[int] = field(default_factory=list)
    reviewed: bool = False

    @property
    def key(self) -> int:
        return min(self.members) if self.members else -1


@dataclass
class Plan:
    """一次聚簇的结果：分组 + 每对的关系 + 用量。"""

    clusters: list[Cluster] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    vetoed: list[int] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)

    def relation_of(self, left: int, right: int) -> str:
        for pair in self.pairs:
            if {pair.left, pair.right} == {left, right}:
                return pair.relation
        return REL_SAME


def _host(value: str) -> str:
    text = (value or "").strip().lower()
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0].split("@")[-1].split(":", 1)[0]


def is_community(url: str, domains: list[str]) -> bool:
    """是否社区来源：讨论算热度但不计入独立来源数。

    委托给 `EditorialSection.is_community`（配置是名单的唯一来源）。
    """
    return EditorialSection(community_domains=list(domains)).is_community(url)


def candidate_pairs(items: list[Candidate], *, floor: float) -> list[tuple[int, int]]:
    """挑出值得让模型判断的配对。

    阈值**故意远低于判重阈值**：这一步是"选谁去问模型"，漏掉的配对模型永远看不到，
    所以宁可多问。除了词集相似度，**共带数字的标识符**（`qwen-image-2.1-turbo`）直接
    入选——它是唯一能跨中英文对齐同一件事的词法证据，而词集相似度在中英混排时会很低。

    结果按"像"的程度**降序**返回：配对总量有预算（`cluster_pair_budget`），超预算时
    被砍掉的是排在后面的，所以排序决定了取舍是否有依据。
    """
    scored: list[tuple[float, int, int]] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            score = _plausibility(left, right, floor)
            if score > 0:
                scored.append((score, left.news_item_id, right.news_item_id))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [(left, right) for _, left, right in scored]


def _plausibility(left: Candidate, right: Candidate, floor: float) -> float:
    """两个条目的"像"的程度；0 表示不值得让模型判断。

    返回分数而不是布尔值，是为了让"配对超预算"时的取舍有依据。标识符相关给最高分：
    `identifiers()` 取的是整段 token，实测「Qwen-Image 2.1 Turbo」切成 ``2.1``、而
    「阿里发布 qwen-image-2.1-turbo」是完整的 ``qwen-image-2.1-turbo``，两边**不相等**
    却说的是同一件事，所以还要做子串包含判断。这只是预筛（召回用），误报的代价是
    多问模型一对，可以接受——但**短碎片不算**（见 `_specific_identifiers`）。
    """
    left_body, right_body = left.body.lower(), right.body.lower()
    left_terms, right_terms = _specific_identifiers(left.body), _specific_identifiers(right.body)
    if left_terms & right_terms:
        return 1.0
    if any(term in right_body for term in left_terms) or any(term in left_body for term in right_terms):
        return 0.9
    left_tokens, right_tokens = set(_tokens(left.body)), set(_tokens(right.body))
    if len(left_tokens) < 3 or len(right_tokens) < 3:
        return 0.0
    overlap = jaccard(left_tokens, right_tokens)
    return overlap if overlap >= floor else 0.0


def _specific_identifiers(text: str) -> set[str]:
    """预筛专用：只保留**够具体**的标识符。

    实测为什么必须过滤：`identifiers()` 会把 `2.1`、`gpt-6`、`1000` 这类短串也取出来，
    而它们毫无识别力——
    - 「JetBrains Releases Mellum2.1」与「Nano Banana终于2.1了」共享 `2.1`，是两件事；
    - 「…突围GPT-6」与「GPT-6.1 Sol 极速版上线」共享 `gpt-6`，也是两件事；
    - `1000 多次 PR` 与另一个标题共享 `1000`。

    这些假阳性不只是浪费模型调用，还会把无关条目推进同一批判定里（模型得在噪声里
    找信号）。要求长度 ≥ 8 且含字母：`qwen-image-2.1-turbo`、`microsoft-decision-1`
    这类真正的产品/版本串照旧命中，短碎片被挡掉。
    """
    return {
        term for term in identifiers(text) if len(term) >= MIN_IDENTIFIER_CHARS and any(ch.isalpha() for ch in term)
    }


def parse_relations(text: str, allowed: set[int]) -> dict[tuple[int, int], str]:
    """把模型输出解析成"配对 → 关系"。只认形状正确的行，越界编号丢掉。

    解析不出来**不报错**：调用方按"不确定"处理（见 `Clusterer.assign`），
    而不是当成"两件事无关"——把模型输出解析失败理解成否定结论，会让本该合并的
    报道各自成事件，进而让日报里出现同一件事的多条。

    每个编号都要在 `allowed` 里：模型偶尔会把示例里的编号抄进结论（提示词里有
    `1,3,same` 这个例子），不校验就会凭空造出一对。
    """
    found: dict[tuple[int, int], str] = {}
    for left, right, relation in _REL_LINE.findall(text or ""):
        a, b, name = int(left), int(right), relation.lower().strip()
        if a == b or a not in allowed or b not in allowed or name not in RELATIONS:
            continue
        found[(min(a, b), max(a, b))] = name
    return found


def summarise(relations: dict[tuple[int, int], str]) -> str:
    """按关系类型统计，便于一眼看出"模型判了什么"而不是只能看到总数。"""
    counts = {name: 0 for name in RELATIONS}
    for relation in relations.values():
        counts[relation] = counts.get(relation, 0) + 1
    return "，".join(f"{name} {count}" for name, count in counts.items() if count)


def parse_vetoes(text: str, total: int) -> set[int]:
    """解析复核阶段被否掉的组号（1 基）。"""
    vetoed: set[int] = set()
    for line in (text or "").splitlines():
        parts = re.split(r"[,，\s]+", line.strip())
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        if _VETO_LINE.match(parts[1].strip()) and 1 <= int(parts[0]) <= total:
            vetoed.add(int(parts[0]) - 1)
    if not vetoed and _VETO_LINE.match((text or "").strip()):
        # 只有一组时模型可能只回一个 `no`
        vetoed.add(0)
    return vetoed


@dataclass
class Clusterer:
    """把报道归到同一件事上：模型判定 → 不确定的合并 → 复核 → 落库。

    三段设计各有原因：

    1. **判定用一次调用处理所有配对**，不是每对调一次。配对数量随条数平方增长，
       逐对调用会立刻失控；一次调用还能让模型在批次内部保持一致的标准。
    2. **个别配对没结论时一律合并**（不是报错，也不是当成"两件事无关"）。这里与小节
       `dedup` 的方向相反，是刻意的：漏合并会产生"同一个事件的两条"直接进日报（正是要
       修的毛病），多合并只是把一条后续进展挂到同一件事上，事件页看起来仍然合理，而且
       下一段的复核专门负责把它们拆开。**但整批都没有结论时不合并**——那说明模型这次
       不可用，把几十条资讯并成一件事比不合并糟得多。
    3. **复核只针对被合并的组**（不是全量）。复核的成本与组数而不是条数成正比，
       而它的唯一任务是把"不确定被合并进来的东西"挑出去。
    """

    llm: LLM
    config: EditorialSection = field(default_factory=EditorialSection)

    def build_pair_prompt(self, items: dict[int, Candidate], pairs: list[tuple[int, int]]) -> str:
        """配对 + 两侧标题摘要。格式要求放末尾（实测放前面模型会先写分析）。"""
        listing = "\n".join(
            f"{left} / {right}：{_brief(items[left])} ‖ {_brief(items[right])}" for left, right in pairs
        )
        return f"{_PAIR_PROMPT}\n{listing}\n{_PAIR_TAIL}"

    def build_review_prompt(self, items: dict[int, Candidate], clusters: list[Cluster]) -> str:
        lines = []
        for number, cluster in enumerate(clusters, 1):
            members = "；".join(_brief(items[member]) for member in cluster.members if member in items)
            lines.append(f"{number}：{members}")
        return f"{_REVIEW_PROMPT}\n" + "\n".join(lines) + f"\n{_REVIEW_TAIL}"

    async def _ask(self, prompt: str) -> tuple[str, Usage | None]:
        """一次模型调用。**必须记录 token 用量**：这个端点是推理模型，推理与正文
        共用输出额度，额度被推理吃光时调用"成功"却返回空正文——不打印用量就只能
        看到"模型没给结论"，无从判断是提示词不行还是额度不够。
        """
        reply = await asyncio.wait_for(
            self.llm.complete([{"role": "user", "content": prompt}], max_tokens=self.config.cluster_max_tokens),
            self.config.cluster_timeout_seconds,
        )
        used = reply.usage() if (reply.prompt_tokens or reply.completion_tokens) else None
        content = reply.content or ""
        if not content.strip():
            logger.warning(
                "模型返回空正文（prompt %d / completion %d token，上限 %d）：推理多半把输出额度吃光了",
                reply.prompt_tokens,
                reply.completion_tokens,
                self.config.cluster_max_tokens,
            )
        return content, used

    async def _judge(
        self, by_id: dict[int, Candidate], pairs: list[tuple[int, int]], plan: Plan
    ) -> dict[tuple[int, int], str]:
        """把配对**分批**交给模型判定，返回"配对 → 关系"。

        为什么必须分批：实测一次给 60 对时，推理模型把输出额度（4000 token）全花在
        推理上，调用耗时 63 秒却返回**空正文**——结论一条都拿不到。批次变小后每批的
        推理量随之变小，正文能正常产出；而且所有配对都判得到，不会像截断那样静默丢掉
        大部分（实测 221 对里只判了 60 对）。

        某一批失败（超时、空正文、解析不出来）只影响那一批：**这一批的配对按"不确定"
        处理**（`_effective` 会合并），而不是让整次聚簇退回"各自成事件"。
        """
        batch = max(self.config.cluster_max_pairs, 1)
        chunks = [pairs[index : index + batch] for index in range(0, len(pairs), batch)]
        relations: dict[tuple[int, int], str] = {}
        empty_batches = 0
        for number, chunk in enumerate(chunks, 1):
            try:
                text, used = await self._ask(self.build_pair_prompt(by_id, chunk))
            except Exception as exc:
                logger.warning("第 %d/%d 批配对判定失败: %s: %s", number, len(chunks), type(exc).__name__, exc)
                empty_batches += 1
                continue
            if used is not None:
                plan.usage.append(used)
            found = parse_relations(text, set(by_id))
            if not found:
                empty_batches += 1
                # 分辨"模型说都不同"与"输出没被解析出来"必须靠原始输出：只看总数
                # 会让人以为是后者，实际可能只是这一批真的没重复
                logger.warning(
                    "第 %d/%d 批没有解析出结论，原始输出: %s", number, len(chunks), (text or "").strip()[:200]
                )
                continue
            relations.update(found)
            logger.info("第 %d/%d 批：%s（%d/%d 对）", number, len(chunks), summarise(found), len(found), len(chunk))
        covered = len(relations)
        logger.info(
            "配对判定完成：%d 批，解析出 %d/%d 对结论，%d 批无结论",
            len(chunks),
            covered,
            len(pairs),
            empty_batches,
        )
        if not relations:
            # 整批都没结论 = 模型这次不可用，按"不做合并"处理（见 `_effective`）
            logger.warning("所有批次都没有结论，本次不做合并")
        return relations

    async def assign(self, items: list[Candidate]) -> Plan:
        """给出聚簇方案；任何模型侧失败都退回"每条各自成事件"。

        退回**不合并**而不是"全并成一条"：模型不可用时把 40 条资讯归成一件事，
        事件页与日报都会变成一团乱麻；各自成事件只是失去合并能力，内容不会丢。
        """
        plan = Plan()
        if len(items) < 2:
            plan.clusters = [Cluster(members=[item.news_item_id]) for item in items]
            return plan
        by_id = {item.news_item_id: item for item in items}
        pairs = candidate_pairs(items, floor=self.config.cluster_pair_floor)
        budget = self.config.cluster_pair_budget
        if len(pairs) > budget:
            # 配对已按相似度降序，砍尾就是砍"最不像的"那些——按编号顺序砍等于随机砍
            logger.warning("候选配对 %d 对超过预算 %d，按相似度保留前 %d 对", len(pairs), budget, budget)
            pairs = pairs[:budget]
        relations: dict[tuple[int, int], str] = {}
        if pairs:
            relations = await self._judge(by_id, pairs, plan)
        plan.pairs = [
            Pair(
                left=left,
                right=right,
                relation=relations.get((left, right), REL_DIFFERENT),
                certain=(left, right) in relations,
            )
            for left, right in pairs
        ]
        clusters = _merge(items, _effective(relations, pairs))
        clusters = await self._review(by_id, clusters, plan)
        plan.clusters = clusters
        logger.info(
            "聚簇：%d 条报道 → %d 个事件（配对 %d，复核否决 %d 组）",
            len(items),
            len(clusters),
            len(pairs),
            len(plan.vetoed),
        )
        return plan

    async def _review(self, by_id: dict[int, Candidate], clusters: list[Cluster], plan: Plan) -> list[Cluster]:
        """复核被合并的组；失败就保留原分组（复核是增强而非必要条件）。"""
        merged = [cluster for cluster in clusters if len(cluster.members) > 1]
        if not merged:
            return clusters
        try:
            text, used = await self._ask(self.build_review_prompt(by_id, merged))
            if used is not None:
                plan.usage.append(used)
        except Exception as exc:
            logger.warning("聚簇复核失败，保留原分组: %s: %s", type(exc).__name__, exc)
            return clusters
        vetoed = parse_vetoes(text, len(merged))
        if not vetoed:
            return clusters
        plan.vetoed = [merged[index].key for index in sorted(vetoed) if index < len(merged)]
        keep: list[Cluster] = []
        for index, cluster in enumerate(merged):
            if index in vetoed:
                # 复核是**专门针对"这一整组是不是一件事"**给出的判断，而判定阶段的
                # 配对结论是在不知道整组构成的情况下逐对给出的，所以复核的结论优先。
                # 组内哪几对仍然成立它没说，无从推断——一并拆开，作为各自独立的事件。
                keep.extend(Cluster(members=[member]) for member in cluster.members)
            else:
                cluster.reviewed = True
                keep.append(cluster)
        singles = [cluster for cluster in clusters if len(cluster.members) == 1]
        return keep + singles


def _merge(items: list[Candidate], relations: dict[tuple[int, int], str]) -> list[Cluster]:
    """按"关系成立"的配对做并查集合并；不成立的（`different`）不参与。

    注意遍历范围要包含**所有出现过的节点**：并查集只把被 union 的一方写进 parent，
    每组的代表节点本身不在里面，只遍历 parent 会把代表漏掉（`dedup.parse_groups` 上
    已经踩过一次，少合并了一条）。
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

    grouped = {item.news_item_id for item in items}
    for (left, right), relation in relations.items():
        if relation in (REL_SAME, REL_FOLLOW_UP):
            union(left, right)
            grouped.add(left)
            grouped.add(right)
    buckets: dict[int, list[int]] = {}
    for node in grouped:
        buckets.setdefault(find(node), []).append(node)
    return [Cluster(members=sorted(members)) for _, members in sorted(buckets.items())]


def _effective(relations: dict[tuple[int, int], str], pairs: list[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """把"模型没给结论"的配对也算成合并——但只在**结论覆盖面足够**时才这么做。

    为什么要合并没结论的配对：漏合并会让同一件事裂成两条各自进日报（正是要修的毛病），
    多合并只是把一条后续进展挂到同一件事上，外观仍合理，而且下一段复核专门负责拆开。

    为什么要卡覆盖面：分批判定时单个批次失败（超时、空正文）只影响那一批，若不加限制，
    "少数批次成功 + 多数批次失败"就会把大批**从未问过模型**的配对当成"同一件事"合并
    ——那比不合并糟得多。所以覆盖面低于 `MIN_COVERAGE` 时，未获结论的配对一律按"不算"
    处理（不合并），宁可漏合并也不凭空造出巨大事件。
    """
    if not relations or not pairs:
        return {}
    if len(relations) / len(pairs) < MIN_COVERAGE:
        logger.warning(
            "配对结论只覆盖 %d/%d（低于 %d%%），未获结论的配对不合并",
            len(relations),
            len(pairs),
            int(MIN_COVERAGE * 100),
        )
        return {key: value for key, value in relations.items()}
    merged = dict(relations)
    for left, right in pairs:
        merged.setdefault((left, right), REL_SAME)
    return merged


def _brief(item: Candidate, *, limit: int = 70) -> str:
    text = item.body.replace("\n", " ")
    return f"[{item.news_item_id}] {text[:limit]}"


def role_of(members: list[int], primary: int, pairs: list[Pair]) -> dict[int, str]:
    """组内每条报道的角色。

    一手报道取**发布时间最早**的那条：后续进展的语义是"这件事有了新信息"，
    而新信息在时间上必然晚于原始报道。这与"质量最高的那条"不同——质量高的
    往往是后出的深度报道，把它当一手会颠倒因果关系。
    """
    roles = {member: ROLE_DUPLICATE for member in members}
    roles[primary] = ROLE_PRIMARY
    for pair in pairs:
        if pair.relation != REL_FOLLOW_UP or not pair.certain:
            continue
        other = pair.right if pair.left == primary else pair.left
        if primary not in (pair.left, pair.right) or other not in roles:
            continue
        roles[other] = ROLE_FOLLOW_UP
    return roles


def pick_primary(items: list[Candidate]) -> int:
    """一手报道：发布时间最早的；都没有时间就取编号最小的（确定性输出）。"""
    if not items:
        return -1
    return min(
        items,
        key=lambda item: (
            item.published_at is None,
            item.published_at or datetime.max.replace(tzinfo=UTC),
            item.news_item_id,
        ),
    ).news_item_id


async def load_candidates(
    db: Database,
    *,
    days: int,
    now: datetime | None = None,
    editorial: EditorialSection | None = None,
) -> list[Candidate]:
    """读取最近 `days` 天内已入库、且**尚未归入任何事件**的报道。

    传入 `editorial` 时同时定信源分级与社区标记。**不能在这里偷懒写成默认值**：
    分级决定入选门槛、社区标记决定"算不算独立来源"，两者错了整条链路的结论都会错
    （所有来源都被当成 T2、社区讨论被当成编辑类来源，热度与精选都会失真）。
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=days)
    async with db.session() as session:
        assigned = select(ReportItem.news_item_id)
        rows = (
            await session.scalars(
                select(NewsItem)
                .where(NewsItem.id.not_in(assigned))
                .where(NewsItem.fetched_at >= since)
                .order_by(NewsItem.id)
            )
        ).all()
    config = editorial
    return [
        Candidate(
            news_item_id=row.id,
            title=row.title or "",
            text=row.summary or "",
            source=row.source or "",
            url=row.url or "",
            published_at=row.published_at,
            tier=config.tier_of(row.source or "", row.url or "") if config else "T2",
            editorial=not config.is_community(row.url or "") if config else True,
        )
        for row in rows
    ]


async def write_plan(db: Database, plan: Plan, items: list[Candidate]) -> dict[str, Any]:
    """把聚簇方案落库。返回统计。

    两条不可协商的规则：

    - **人工锁定的归属永不覆盖**（`story.locked`）。归组会不断重跑，人工改过一次
      又被改回去，等于人改了没用。
    - 已在某个事件里的报道**不重复写入**（`(story_id, news_item_id)` 唯一）。
    """
    by_id = {item.news_item_id: item for item in items}
    stats = {"stories": 0, "items": 0, "skipped_locked": 0, "skipped_assigned": 0}
    async with db.session() as session:
        assigned = set((await session.scalars(select(ReportItem.news_item_id))).all())
        locked_ids = set(
            (
                await session.scalars(
                    select(ReportItem.news_item_id)
                    .join(Story, Story.id == ReportItem.story_id)
                    .where(Story.locked.is_(True))
                )
            ).all()
        )
        for cluster in plan.clusters:
            members = [member for member in cluster.members if member in by_id]
            if not members:
                continue
            locked = [member for member in members if member in locked_ids]
            if locked:
                stats["skipped_locked"] += len(locked)
                logger.info("事件包含人工锁定的报道，跳过其中的 %d 条", len(locked))
            open_members = [member for member in members if member not in locked_ids]
            if not open_members:
                continue
            # 已在某个事件里的报道不重复写入（`(story_id, news_item_id)` 唯一）。
            # 但**角色仍要按整组算**：一手报道取全组最早的一条，若只拿新成员算,
            # 后到的报道会把自己标成 primary，与已有的一手报道冲突。
            fresh = [member for member in open_members if member not in assigned]
            if not fresh:
                stats["skipped_assigned"] += len(open_members)
                continue
            group = [by_id[member] for member in members]
            story = await _match_story(session, members)
            if story is None:
                story = Story(title=group[0].title, first_seen_at=_earliest(group), last_seen_at=_latest(group))
                session.add(story)
                await session.flush()
                stats["stories"] += 1
            else:
                story.last_seen_at = max(_as_utc(story.last_seen_at), _latest(group))
            roles = role_of(open_members, pick_primary(group), plan.pairs)
            for member in fresh:
                item = by_id[member]
                session.add(
                    ReportItem(
                        story_id=story.id,
                        news_item_id=member,
                        participant=item.participant,
                        tier=item.tier,
                        role=roles.get(member, ROLE_DUPLICATE),
                        editorial=item.editorial,
                        observed_at=_as_utc(item.published_at) if item.published_at else datetime.now(UTC),
                    )
                )
                stats["items"] += 1
            # 事件的时间口径必须与逐条报道**同一把尺子**：上面按 `published_at` 算
            # `last_seen_at`，而详情页逐行显示的是 `observed_at`，发布时间为空的条目
            # 在 `observed_at` 上会回退成写入时刻。混用两套时钟的结果是头部写「最新进展
            # 10月12日 14:44」、下面那条报道却是「10月13日 11:24」——同一个事件自相矛盾。
            # 改为从实际落库的报道推导，一致性由构造保证。
            await session.flush()
            observed = list(
                (await session.scalars(select(ReportItem.observed_at).where(ReportItem.story_id == story.id))).all()
            )
            if observed:
                moments = [row if row.tzinfo else row.replace(tzinfo=UTC) for row in observed]
                story.first_seen_at = min(moments)
                story.last_seen_at = max(moments)
                if story.title != group[0].title:
                    continue
        await session.commit()
    return stats


async def _match_story(session: Any, members: list[int]) -> Story | None:
    """组里有任一成员已属于某个事件，就并入那个事件（取最早创建的一个）。

    按成员反查比按标题相似度匹配可靠得多：标题措辞会变，而"这条报道已经在这个事件里"
    是确定的事实。
    """
    existing = (
        await session.scalars(
            select(ReportItem).where(ReportItem.news_item_id.in_(members)).order_by(ReportItem.story_id)
        )
    ).all()
    if not existing:
        return None
    return await session.get(Story, existing[0].story_id)


def _as_utc(value: datetime) -> datetime:
    """SQLite 的 `DateTime(timezone=True)` 不保留时区，读回来是 naive.

    不归一化就会在读-写往返里炸出 "can't compare offset-naive and offset-aware"。
    约定 naive 一律按 UTC 解释——写进去的就是 UTC。
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _earliest(group: list[Candidate]) -> datetime:
    stamps = [_as_utc(item.published_at) for item in group if item.published_at is not None]
    return min(stamps) if stamps else datetime.now(UTC)


def _latest(group: list[Candidate]) -> datetime:
    stamps = [_as_utc(item.published_at) for item in group if item.published_at is not None]
    return max(stamps) if stamps else datetime.now(UTC)
