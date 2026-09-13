from app.tag_utils import merge_tags, normalize_tag_key


class TestNormalizeTagKey:
    """测试标签规范化功能"""

    def test_basic_normalization(self):
        """基本的大小写和空格处理"""
        assert normalize_tag_key("Android") == "android"
        assert normalize_tag_key("ANDROID") == "android"
        assert normalize_tag_key("AnDrOiD") == "android"

    def test_whitespace_handling(self):
        """空格处理"""
        assert normalize_tag_key("  Android  ") == "android"
        assert normalize_tag_key("Web  App") == "web app"
        assert normalize_tag_key("Web\t\tApp") == "web app"
        assert normalize_tag_key("Web\nApp") == "web app"

    def test_fullwidth_to_halfwidth(self):
        """全角转半角"""
        assert normalize_tag_key("Ａｎｄｒｏｉｄ") == "android"
        assert normalize_tag_key("ＷＥＢ") == "web"
        assert normalize_tag_key("Android（全角括号）") == "android(全角括号)"

    def test_empty_and_invalid_input(self):
        """空值和无效输入"""
        assert normalize_tag_key("") == ""
        assert normalize_tag_key("   ") == ""
        assert normalize_tag_key(None) == ""
        assert normalize_tag_key(123) == ""

    def test_preserves_internal_spaces(self):
        """保留内部有意义的空格"""
        assert normalize_tag_key("Web App") == "web app"
        assert normalize_tag_key("Visual Novel") == "visual novel"
        # 但多余空格会被合并
        assert normalize_tag_key("Web    App") == "web app"

    def test_unicode_normalization(self):
        """Unicode 规范化"""
        # NFKC 会统一不同的Unicode表示
        assert normalize_tag_key("café") == normalize_tag_key("café")


class TestMergeTags:
    """测试标签合并功能"""

    def test_merge_identical_tags(self):
        """合并相同标签（不同大小写）"""
        result = merge_tags(["Android"], ["android", "ANDROID"], {})
        assert len(result) == 1
        assert "android" in result
        # 第一次出现的形式被保留
        assert result["android"] == "Android"

    def test_merge_with_tag_names_dict(self):
        """使用 tag_names_dict 的规范名称"""
        tag_names = {"android": "Android", "web": "Web"}
        result = merge_tags([], ["android", "ANDROID", "web"], tag_names)
        assert len(result) == 2
        assert result["android"] == "Android"
        assert result["web"] == "Web"

    def test_merge_fullwidth_tags(self):
        """合并全角标签"""
        result = merge_tags(["Android"], ["Ａｎｄｒｏｉｄ"], {})
        assert len(result) == 1
        assert "android" in result

    def test_merge_with_whitespace_variations(self):
        """合并带空格变化的标签"""
        result = merge_tags(["Android"], ["  Android  ", " Android"], {})
        assert len(result) == 1
        assert "android" in result

    def test_preserves_neodb_canonical_names(self):
        """NeoDB 的规范名称优先"""
        tag_names = {"android": "Android"}  # NeoDB 已有的标签
        result = merge_tags(
            ["ANDROID"],  # 已有标签（大写）
            ["android", "Andriod"],  # 新增标签
            tag_names,
        )
        # "android" 和 "ANDROID" 合并，使用已有的 "ANDROID"
        assert result["android"] == "ANDROID"
        # "Andriod" 是拼写错误，会通过模糊匹配自动合并到 "Android"
        # 所以最终只有一个 "android" 键
        assert len(result) == 1

    def test_empty_tags_ignored(self):
        """忽略空标签"""
        result = merge_tags(["Android", "", "  ", None], ["Web", ""], {})
        assert len(result) == 2
        assert "android" in result
        assert "web" in result

    def test_maintains_display_form(self):
        """保持显示形式"""
        result = merge_tags(["Web App"], ["WEB APP"], {})
        assert len(result) == 1
        # 第一次出现的形式被保留
        assert result["web app"] == "Web App"

    def test_real_world_example(self):
        """真实场景：合并 Bangumi 和 NeoDB 标签"""
        # NeoDB 已有的标签（规范形式）
        neodb_tags = {"android": "Android", "游戏": "游戏"}

        # 用户在 Bangumi 的标签（各种变体）
        bangumi_tags = ["ANDROID", "  Android  ", "游戏", "AVG"]

        # NeoDB 当前收藏的标签
        current_tags = ["Web"]

        result = merge_tags(current_tags, bangumi_tags, neodb_tags)

        # 应该有 4 个不同的标签
        assert len(result) == 4
        # Android 使用 NeoDB 的规范形式
        assert result["android"] == "Android"
        # 游戏 使用 NeoDB 的规范形式
        assert result["游戏"] == "游戏"
        # AVG 是新标签
        assert result["avg"] == "AVG"
        # Web 保留
        assert result["web"] == "Web"
