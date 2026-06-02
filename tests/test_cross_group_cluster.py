from chat_daily_tg.cross_group_cluster import (
    build_cluster_context,
    cluster_cross_group_topics,
    validate_clusters_in_output,
)


def test_clusters_equivalent_wechat_and_telegram_messages_as_cross_group_topic():
    groups = [
        (
            "微信 / OpenCLI 交流群",
            """
# 微信群导出

2026-04-30 10:43
**Alice**: Mimo V2.5 Pro 评测接近 Sonnet，稳定性优于 K2.6，TTS 模型效果惊艳
""",
        ),
        (
            "Telegram / CuiMao爱学习",
            """
# Telegram: CuiMao爱学习

[Telegram / CuiMao爱学习 / 10:47 / Bob] Mimo V2.5 Pro 评测接近 Sonnet，稳定性优于 K2.6，TTS 模型效果惊艳
""",
        ),
    ]

    clusters = cluster_cross_group_topics(groups)
    cross_clusters = [c for c in clusters if c.is_cross_group]

    assert len(cross_clusters) == 1
    assert [s["group"] for s in cross_clusters[0].sources] == [
        "微信 / OpenCLI 交流群",
        "Telegram / CuiMao爱学习",
    ]
    assert [s["time"] for s in cross_clusters[0].sources] == ["10:43", "10:47"]


def test_cluster_context_tells_llm_to_merge_cross_source_topic():
    clusters = cluster_cross_group_topics([
        (
            "微信 / OpenCLI 交流群",
            "2026-04-30 10:43\n**Alice**: Claude 4.7 变啰嗦，GPT 5.5 更强但更耗 token",
        ),
        (
            "Telegram / CuiMao爱学习",
            "[Telegram / CuiMao爱学习 / 10:47 / Bob] Claude 4.7 变啰嗦，GPT 5.5 更强但更耗 token",
        ),
    ])

    context = build_cluster_context(clusters)

    assert "跨群确认" in context
    assert "微信 / OpenCLI 交流群 / 10:43" in context
    assert "Telegram / CuiMao爱学习 / 10:47" in context


def test_validate_clusters_warns_when_merged_output_omits_one_source():
    clusters = cluster_cross_group_topics([
        (
            "微信 / OpenCLI 交流群",
            "2026-04-30 10:43\n**Alice**: Mac mini 养龙虾热度退潮，很多人买完发现用处不大",
        ),
        (
            "Telegram / 电丸朱氏会社",
            "[Telegram / 电丸朱氏会社 / 10:47 / Bob] Mac mini 养龙虾热度退潮，很多人买完发现用处不大",
        ),
    ])

    warnings = validate_clusters_in_output(
        clusters,
        "- Mac mini 养龙虾开始退潮，跟风买家发现用处不大（OpenCLI 交流群 / 10:43）",
    )

    assert warnings
    assert "只标注了 1 个来源" in warnings[0]


def test_telegram_prefix_is_not_clustered_as_message_content():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 贝利知识星球VIP群❤️",
            "2026-04-30 17:03\n**Alice**: 【重要通知】GOPAY將開放予全部老用戶使用",
        ),
        (
            "Telegram / 电丸朱氏会社",
            "[Telegram / 电丸朱氏会社 / 08:57 / JoKeR 哎呦喂呀] 通知：",
        ),
    ])

    assert not [c for c in clusters if c.is_cross_group]


def test_short_telegram_reply_does_not_match_unrelated_long_wechat_sentence():
    clusters = cluster_cross_group_topics([
        (
            "微信 / 贝利知识星球VIP群❤️",
            "2026-04-30 04:13\n**Alice**: 但是我国内直接链学校的vpn用cc是不是感觉会不容易封一点",
        ),
        (
            "Telegram / 电丸朱氏会社",
            "[Telegram / 电丸朱氏会社 / 06:31 / Hao Liyou] 不容易",
        ),
    ])

    assert not [c for c in clusters if c.is_cross_group]
