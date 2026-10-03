import difflib
import random

from chat_daily_tg.cross_group_cluster import (
    CrossGroupCluster,
    _extract_candidate_topics,
    _normalize,
    _signature,
    build_cluster_context,
    cluster_cross_group_topics,
    validate_clusters_in_output,
)


def test_clusters_equivalent_wechat_and_telegram_messages_as_cross_group_topic():
    groups = [
        (
            "微信 / 示例微信群A",
            """
# 微信群导出

2026-04-30 10:43
**Alice**: Mimo V2.5 Pro 评测接近 Sonnet，稳定性优于 K2.6，TTS 模型效果惊艳
""",
        ),
        (
            "Telegram / 示例TG群A",
            """
# Telegram: 示例TG群A

[Telegram / 示例TG群A / 10:47 / Bob] Mimo V2.5 Pro 评测接近 Sonnet，稳定性优于 K2.6，TTS 模型效果惊艳
""",
        ),
    ]

    clusters = cluster_cross_group_topics(groups)
    cross_clusters = [c for c in clusters if c.is_cross_group]

    assert len(cross_clusters) == 1
    assert [s["group"] for s in cross_clusters[0].sources] == [
        "微信 / 示例微信群A",
        "Telegram / 示例TG群A",
    ]
    assert [s["time"] for s in cross_clusters[0].sources] == ["10:43", "10:47"]


def test_cluster_context_tells_llm_to_merge_cross_source_topic():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 示例微信群A",
            "2026-04-30 10:43\n**Alice**: Claude 4.7 变啰嗦，GPT 5.5 更强但更耗 token",
        ),
        (
            "Telegram / 示例TG群A",
            "[Telegram / 示例TG群A / 10:47 / Bob] Claude 4.7 变啰嗦，GPT 5.5 更强但更耗 token",
        ),
    ])

    context = build_cluster_context(clusters)

    assert "跨群确认" in context
    assert "微信 / 示例微信群A / 10:43" in context
    assert "Telegram / 示例TG群A / 10:47" in context


def test_validate_clusters_warns_when_merged_output_omits_one_source():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 示例微信群A",
            "2026-04-30 10:43\n**Alice**: Mac mini 养龙虾热度退潮，很多人买完发现用处不大",
        ),
        (
            "Telegram / 示例TG群B",
            "[Telegram / 示例TG群B / 10:47 / Bob] Mac mini 养龙虾热度退潮，很多人买完发现用处不大",
        ),
    ])

    warnings = validate_clusters_in_output(
        clusters,
        "- Mac mini 养龙虾开始退潮，跟风买家发现用处不大（示例微信群A / 10:43）",
    )

    assert warnings
    assert "只标注了 1 个来源" in warnings[0]


def test_telegram_prefix_is_not_clustered_as_message_content():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 示例微信群B",
            "2026-04-30 17:03\n**Alice**: 【重要通知】某支付工具将开放给全部老用户使用",
        ),
        (
            "Telegram / 示例TG群B",
            "[Telegram / 示例TG群B / 08:57 / 样例用户F] 通知：",
        ),
    ])

    assert not [c for c in clusters if c.is_cross_group]


def test_short_telegram_reply_does_not_match_unrelated_long_wechat_sentence():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 示例微信群B",
            "2026-04-30 04:13\n**Alice**: 但是我在国内直接连学校的网络用某工具是不是感觉会不容易封一点",
        ),
        (
            "Telegram / 示例TG群B",
            "[Telegram / 示例TG群B / 06:31 / 样例用户G] 不容易",
        ),
    ])

    assert not [c for c in clusters if c.is_cross_group]


def test_containment_match_survives_large_length_difference():
    # containment (0.92) is checked before the length-ratio gate; a short line
    # fully contained in a much longer one must still cluster cross-group.
    long_line = "苹果发布新一代自研芯片性能提升明显功耗下降不少"
    clusters = cluster_cross_group_topics([
        ("微信 / A", f"2026-04-30 10:00\n**Alice**: {long_line}"),
        ("Telegram / B",
         f"[Telegram / B / 10:05 / Bob] {long_line}评测者补充了大量实机对比数据说明结论可信"),
    ])

    assert [c for c in clusters if c.is_cross_group]


# --------------------------------------------------------------------------- #
# parity with the pre-optimization implementation (2026-08-16 perf pass)

def _reference_cluster_cross_group_topics(groups_with_content, similarity_threshold=0.85):
    """The pre-gate implementation, pinned verbatim: per-pair normalization,
    containment 0.92, plain SequenceMatcher.ratio, same greedy order."""

    def similarity(a: str, b: str) -> float:
        na, nb = _normalize(a), _normalize(b)
        if not na or not nb:
            return 0.0
        if na in nb or nb in na:
            return 0.92
        return difflib.SequenceMatcher(None, na, nb).ratio()

    all_topics = []
    for group_name, content in groups_with_content:
        all_topics.extend(_extract_candidate_topics(content, group_name))
    if not all_topics:
        return []

    clusters = []
    used = set()
    for i, topic in enumerate(all_topics):
        if i in used:
            continue
        cluster = [topic]
        used.add(i)
        for j, other in enumerate(all_topics):
            if j in used or j == i:
                continue
            if similarity(topic.text, other.text) >= similarity_threshold:
                cluster.append(other)
                used.add(j)
        clusters.append(cluster)

    result = []
    for group in clusters:
        unique_sources = []
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


_CJK_POOL = (
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动"
    "同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自"
    "二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日"
    "那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变"
    "条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总"
    "次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指"
)


def _synth_groups(seed: int = 20260816):
    """~200 extractable lines over 3 groups: exact repeats (containment path),
    lightly mutated repeats (ratio path, some near the 0.85 threshold) and
    unique noise (gate-skippable majority)."""
    rng = random.Random(seed)

    def sentence(lo=15, hi=60) -> str:
        return "".join(rng.choice(_CJK_POOL) for _ in range(rng.randint(lo, hi)))

    base = [sentence() for _ in range(90)]
    groups = []
    for g, n_lines in enumerate((70, 70, 60)):
        lines = [f"# 群{g} 导出"]
        for _ in range(n_lines):
            roll = rng.random()
            source = rng.choice(base)
            if roll < 0.30:
                line = source
            elif roll < 0.50:
                mutated = list(source)
                for _ in range(rng.randint(1, 3)):
                    mutated[rng.randrange(len(mutated))] = rng.choice(_CJK_POOL)
                line = "".join(mutated)
            elif roll < 0.65:
                line = source + sentence(3, 10)
            else:
                line = sentence()
            lines.append(line[:120])
        groups.append((f"群{g}", "\n".join(lines)))
    return groups


def test_optimized_clustering_matches_reference_on_synthetic_corpus():
    groups = _synth_groups()
    fast = cluster_cross_group_topics(groups)
    reference = _reference_cluster_cross_group_topics(groups)

    assert fast == reference
    assert len(fast) > 40  # the corpus produced a meaningful workload
    assert any(c.is_cross_group for c in fast)
    assert any(not c.is_cross_group for c in fast)
