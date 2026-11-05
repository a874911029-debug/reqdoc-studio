"""Regression cases for local organization and preservation of document data."""
import unittest
from unittest.mock import patch

from reqdoc_organize import (
    OrganizeOptions, OrganizationCancelled, organize_with_report, parse_rules,
)


class OrganizeTests(unittest.TestCase):
    def test_unheaded_chinese_notes_group_by_repeated_topic_terms(self):
        notes = ["场景灯光需要使用暖黄色光源。", "灯光照明应该突出入口的位置。",
                 "砖墙材质需要保留粗糙纹理。", "墙面材质可以增加砖块纹理。", "明天买牛奶。"]
        result = organize_with_report([("散记.txt", "\n\n".join(notes))])
        topics = [block.topic for block in result.blocks]
        self.assertEqual(topics[0], topics[1])
        self.assertEqual(topics[2], topics[3])
        self.assertNotEqual(topics[0], topics[2])
        self.assertEqual(topics[4], "待归类")
        for note in notes:
            self.assertIn(note, result.content)

    def test_rule_aliases_group_different_wording_and_control_order(self):
        options = OrganizeOptions(rules_text="材质 = 材质, 贴图, 纹理\n灯光 = 灯光, 照明, 光照")
        result = organize_with_report([("随记", "照明要突出建筑入口。\n\n贴图使用石材纹理。\n\n光照采用暖色调。")], options)
        self.assertEqual([block.topic for block in result.blocks], ["灯光", "材质", "灯光"])
        self.assertLess(result.content.index("## 材质"), result.content.index("## 灯光"))
        self.assertIn("关键词规则", result.report)

    def test_equal_rule_matches_remain_available_for_review(self):
        options = OrganizeOptions(rules_text="灯光 = 灯光\n材质 = 材质")
        result = organize_with_report([("甲", "灯光和材质要一同考虑。")], options)
        self.assertEqual(result.blocks[0].topic, "待归类")
        self.assertIn("并列命中", result.report)

    def test_whitespace_duplicates_merge_but_numbers_and_negation_remain(self):
        result = organize_with_report([
            ("甲.md", "## 预算\n\n预算上限为 100 元，必须在本地保存全部数据。"),
            ("乙.md", "## 预算\n\n预算上限为  100 元，必须在本地保存全部数据。\n\n预算上限为 200 元，必须在本地保存全部数据。\n\n预算上限为 100 元，禁止在本地保存全部数据。"),
        ])
        self.assertEqual(result.stats["duplicates"], 1)
        self.assertEqual(result.stats["output_blocks"], 3)
        self.assertIn("甲.md", result.blocks[0].origins[0])
        self.assertIn("乙.md", result.blocks[0].origins[1])
        self.assertIn("数值或日期不同", result.report)
        self.assertIn("否定或限制表达不同", result.report)
        self.assertIn("200 元", result.content)
        self.assertIn("禁止在本地", result.content)

    def test_parent_contexts_and_code_case_are_preserved(self):
        result = organize_with_report([
            ("角色", "## 角色\n### 颜色\n红色"),
            ("场景", "## 场景\n### 颜色\n蓝色"),
        ])
        self.assertIn("角色 / 颜色", result.content)
        self.assertIn("场景 / 颜色", result.content)
        code = "```python\nprint('A')\n```\n\n```python\nprint('a')\n```"
        result = organize_with_report([("code", code)])
        self.assertEqual(result.stats["output_blocks"], 2)

    def test_tables_fences_lists_and_images_survive_splitting(self):
        table = "| 名称 | 数值 |\n| --- | --- |\n| 大小 | 10 |"
        code = "```md\n# 代码内标题\n\n灯光：不是章节\n```"
        steps = "1. 先画白模\n2. 然后打光\n   - 保留层次"
        image = "![结构参考](images/中文参考.png)"
        text = f"## 方案\n\n{table}\n\n{code}\n\n{steps}\n\n{image}"
        result = organize_with_report([("中文目录/方案.md", text)])
        for item in (table, code, steps, image):
            self.assertIn(item, result.content)
        kinds = [block.kind for block in result.blocks]
        self.assertIn("table", kinds)
        self.assertIn("code", kinds)
        self.assertIn("list", kinds)
        for block in result.blocks:
            if block.text:
                self.assertEqual("\n".join(text.splitlines()[block.start - 1:block.end]), block.text)

    def test_reference_labels_from_different_sources_do_not_collide(self):
        result = organize_with_report([
            ("甲", "[参考][ref]\n\n[ref]: https://example.com/a\n\n[^n]\n\n[^n]: 第一条说明"),
            ("乙", "[参考][ref]\n\n[ref]: https://example.com/b\n\n[^n]\n\n[^n]: 第二条说明"),
        ])
        for expected in ("[参考][reqdoc-s1-ref]", "[参考][reqdoc-s2-ref]", "[^reqdoc-s1-n]", "[^reqdoc-s2-n]",
                         "[reqdoc-s1-ref]: https://example.com/a", "[reqdoc-s2-ref]: https://example.com/b"):
            self.assertIn(expected, result.content)

    def test_source_mode_keeps_order_and_duplicate_option_is_respected(self):
        text = "## 背景\n\n重复内容\n\n## 目标\n\n重复内容"
        options = OrganizeOptions(mode="source", merge_duplicates=False)
        result = organize_with_report([("甲", text), ("乙", text)], options)
        self.assertEqual([block.source for block in result.blocks], ["甲", "甲", "乙", "乙"])
        self.assertEqual(result.stats["duplicates"], 0)

    def test_heading_aliases_and_Chinese_sections_are_recognized(self):
        result = organize_with_report([("甲", "一、项目背景\n\n正文甲"), ("乙", "背景：\n\n正文乙")])
        self.assertEqual({block.topic for block in result.blocks}, {"背景"})
        result = organize_with_report([("甲", "只有标题\n===\n\n## 空章节")])
        self.assertIn("只有标题", result.content)
        self.assertIn("空章节", result.content)

    def test_structure_mode_ignores_content_keyword_reassignment(self):
        result = organize_with_report([("甲", "## 目标\n\n这里有灯光")],
                                      OrganizeOptions(mode="structure", rules_text="照明 = 灯光"))
        self.assertEqual(result.blocks[0].topic, "目标")

    def test_invalid_rules_empty_input_and_cancel_produce_clear_errors(self):
        for value in ("没有等号", "主题 = a", "主题 = 灯光\n主题 = 材质"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_rules(value)
        with self.assertRaises(ValueError):
            organize_with_report([("甲", "\n\n")])
        with self.assertRaises(OrganizationCancelled):
            organize_with_report([("甲", "正文")], cancel=lambda: True)
        with patch("reqdoc_organize.MAX_INPUT_BYTES", 5), self.assertRaises(ValueError):
            organize_with_report([("甲", "超过限制")])

    def test_unclosed_fence_gets_reported_and_does_not_swallow_next_source(self):
        result = organize_with_report([("甲", "```\n代码"), ("乙", "## 标题\n\n另一份正文")])
        self.assertIn("补齐代码围栏", result.report)
        self.assertIn("另一份正文", result.content)
        self.assertEqual(result.content.count("```"), 2)

    def test_common_function_words_do_not_force_topic_groups(self):
        result = organize_with_report([("甲", "需要使用红色颜料。\n\n需要使用本地磁盘。\n\n需要使用微波炉。")])
        self.assertEqual(result.stats["unclassified"], 3)

    def test_inline_label_changes_split_mixed_notes_but_keep_text(self):
        result = organize_with_report([("甲", "灯光：入口偏暖；材质：石头粗糙。")])
        self.assertEqual([block.topic for block in result.blocks], ["灯光", "材质"])
        self.assertIn("灯光：入口偏暖；", result.content)
        self.assertIn("材质：石头粗糙。", result.content)

    def test_chinese_ordered_steps_stay_together(self):
        text = "一、先打开软件\n二、再导入资料\n三、最后点击保存"
        result = organize_with_report([("甲", text)])
        self.assertIn(text, result.content)
        self.assertEqual(len(result.blocks), 1)

    def test_english_rules_match_next_to_chinese_and_skip_word_substrings(self):
        result = organize_with_report([("甲", "使用UE5做白模。\n\nAI接口用于分析。\n\n邮件使用mailing功能。")],
                                      OrganizeOptions(rules_text="虚幻 = UE5\n模型 = AI"))
        self.assertEqual([block.topic for block in result.blocks], ["虚幻", "模型", "待归类"])

    def test_shared_website_domain_does_not_force_note_similarity(self):
        result = organize_with_report([("甲", "红色油画颜料 https://example.com/a\n\n本地磁盘空间 https://example.com/b")])
        self.assertEqual(result.stats["unclassified"], 2)

    def test_list_indentation_difference_keeps_both_hierarchies(self):
        result = organize_with_report([
            ("甲", "## 列表\n\n- 主项\n  - 子项"),
            ("乙", "## 列表\n\n- 主项\n    - 子项"),
        ])
        self.assertEqual(result.stats["duplicates"], 0)
        self.assertIn("- 主项\n  - 子项", result.content)
        self.assertIn("- 主项\n    - 子项", result.content)


if __name__ == "__main__":
    unittest.main()
