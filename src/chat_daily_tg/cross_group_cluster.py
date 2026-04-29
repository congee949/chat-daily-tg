from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TopicSignature:
    text: str
    source_group: str
    timestamp: str
    original_indices: tuple[int, ...] = ()


@dataclass
class CrossGroupCluster:
    cluster_id: str
    title: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    is_cross_group: bool = False

    def to_prompt_text(self) -> str:
        if not self.is_cross_group:
            src = self.sources[0]
            return f"- 「{self.title}」仅出现在 {src['group']} / {src['time']}"
        parts = [f"{s['group']} / {s['time']}" for s in self.sources]
        return f"- 「{self.title}」跨群确认：{'；'.join(parts)}"


def _normalize(text: str) -> str:
    """Normalize text for signature comparison."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"[^一-鿿\w]", "", text)
    return text.lower()[:80]


def _signature(text: str) -> str:
    """Create a stable signature for a topic text."""
    norm = _normalize(text)
    if len(norm) < 8:
        return norm
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def _extract_candidate_topics(markdown: str, group_name: str) -> list[TopicSignature]:
    """Extract short topic signatures from a group's markdown export."""
    topics: list[TopicSignature] = []
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith(">"):
            continue
        if len(line) < 15 or len(line) > 120:
            continue
        # Skip lines that look like sender prefixes, XML fragments, or pure noise
        if line.startswith("**") and line.endswith("**"):
            continue
        if "<?xml" in line or "<msg>" in line or "<img " in line or "<emoticon" in line:
            continue
        if re.search(r"^[\s*.<>]+$", line):
            continue
        # Strip sender prefix like "**name**: content" to get the actual content
        content = re.sub(r"^\*\*[^*]+\*\*:\s*", "", line)
        if len(content) < 10:
            continue
        # Try to find a timestamp nearby
        ts = ""
        for j in range(max(0, i - 5), min(len(lines), i + 5)):
            m = re.search(r"(\d{2}:\d{2})", lines[j])
            if m:
                ts = m.group(1)
                break
        topics.append(TopicSignature(
            text=content,
            source_group=group_name,
            timestamp=ts,
            original_indices=(i,),
        ))
    return topics


def _similarity(a: str, b: str) -> float:
    """Compute text similarity between two topic signatures."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    # Fast containment check
    if na in nb or nb in na:
        return 0.92
    return difflib.SequenceMatcher(None, na, nb).ratio()


def cluster_cross_group_topics(
    groups_with_content: list[tuple[str, str]],
    similarity_threshold: float = 0.85,
) -> list[CrossGroupCluster]:
    """Cluster similar topics across groups for pre-deduplication.

    Returns clusters where each cluster may contain mentions from one or multiple groups.
    """
    all_topics: list[TopicSignature] = []
    for group_name, content in groups_with_content:
        topics = _extract_candidate_topics(content, group_name)
        all_topics.extend(topics)

    if not all_topics:
        return []

    # Greedy clustering
    clusters: list[list[TopicSignature]] = []
    used = set()

    for i, topic in enumerate(all_topics):
        if i in used:
            continue
        cluster = [topic]
        used.add(i)
        for j, other in enumerate(all_topics):
            if j in used or j == i:
                continue
            if _similarity(topic.text, other.text) >= similarity_threshold:
                cluster.append(other)
                used.add(j)
        clusters.append(cluster)

    result: list[CrossGroupCluster] = []
    for group in clusters:
        unique_sources: list[dict[str, Any]] = []
        seen_groups = set()
        for t in group:
            if t.source_group not in seen_groups:
                seen_groups.add(t.source_group)
                unique_sources.append({
                    "group": t.source_group,
                    "time": t.timestamp,
                    "text_snippet": t.text[:60],
                })
        title = group[0].text[:40]
        result.append(CrossGroupCluster(
            cluster_id=_signature(title),
            title=title,
            sources=unique_sources,
            is_cross_group=len(unique_sources) > 1,
        ))

    return result


def build_cluster_context(clusters: list[CrossGroupCluster]) -> str:
    """Build a markdown block for the LLM prompt."""
    if not clusters:
        return ""
    lines = ["## 跨群话题聚类（预处理结果）", ""]
    cross = [c for c in clusters if c.is_cross_group]
    single = [c for c in clusters if not c.is_cross_group]
    if cross:
        lines.append("以下话题在多个群同时出现，已确认跨群关联：")
        for c in cross:
            lines.append(c.to_prompt_text())
        lines.append("")
    if single:
        lines.append(f"其余 {len(single)} 个话题为单群独有。")
        lines.append("")
    return "\n".join(lines)


def validate_clusters_in_output(
    clusters: list[CrossGroupCluster],
    concise_md: str,
) -> list[str]:
    """Post-hoc validation: check if cross-group topics were merged correctly."""
    warnings: list[str] = []
    for c in clusters:
        if not c.is_cross_group:
            continue
        group_names = [s["group"] for s in c.sources]
        # Check if the concise output mentions this cluster with all expected groups
        found_groups = 0
        for g in group_names:
            # Use concise_source_label logic: strip platform prefix
            label = g.split("/")[-1].strip() if "/" in g else g
            if label in concise_md:
                found_groups += 1
        if found_groups < len(group_names) and len(group_names) > 1:
            warnings.append(
                f"跨群话题「{c.title}」在 {len(group_names)} 个群出现，"
                f"但精简版只标注了 {found_groups} 个来源，建议合并尾注"
            )
    return warnings
