"""标签规范化和合并工具"""
import unicodedata


def normalize_tag_key(tag):
    """将标签规范化为用于匹配的键。

    处理：
    - 大小写统一
    - 去除首尾空格
    - 全角转半角
    - 合并内部多余空格

    Args:
        tag: 原始标签字符串

    Returns:
        规范化后的键，用于标签去重
    """
    if not isinstance(tag, str):
        return ""

    # 去除首尾空格
    tag = tag.strip()

    # 全角转半角
    # NFKC 会将全角字符转换为半角等价字符
    tag = unicodedata.normalize('NFKC', tag)

    # 统一大小写
    tag = tag.casefold()

    # 合并多余空格（保留单个空格，因为"Web App"和"WebApp"可能是不同的标签）
    tag = ' '.join(tag.split())

    return tag


def damerau_levenshtein_distance(s1, s2):
    """计算两个字符串的 Damerau-Levenshtein 距离。

    在标准编辑距离基础上，增加了相邻字符交换操作。
    这可以更好地检测常见的拼写错误，如 "Android" vs "Andriod"。

    Args:
        s1: 第一个字符串
        s2: 第二个字符串

    Returns:
        编辑距离（插入、删除、替换、相邻交换）
    """
    len1, len2 = len(s1), len(s2)

    # 创建距离矩阵
    # 需要额外一行一列来处理空字符串的情况
    d = [[0] * (len2 + 1) for _ in range(len1 + 1)]

    # 初始化第一行和第一列
    for i in range(len1 + 1):
        d[i][0] = i
    for j in range(len2 + 1):
        d[0][j] = j

    # 计算距离
    for i in range(1, len1 + 1):
        for j in range(1, len2 + 1):
            cost = 0 if s1[i-1] == s2[j-1] else 1

            d[i][j] = min(
                d[i-1][j] + 1,      # 删除
                d[i][j-1] + 1,      # 插入
                d[i-1][j-1] + cost  # 替换
            )

            # 检查相邻字符交换
            if i > 1 and j > 1 and s1[i-1] == s2[j-2] and s1[i-2] == s2[j-1]:
                d[i][j] = min(d[i][j], d[i-2][j-2] + 1)  # 交换

    return d[len1][len2]


def levenshtein_distance(s1, s2):
    """计算两个字符串的编辑距离（Levenshtein距离）。

    Args:
        s1: 第一个字符串
        s2: 第二个字符串

    Returns:
        编辑距离（需要多少次插入、删除、替换操作才能将s1转换为s2）
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # 插入、删除、替换的成本
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def find_similar_tag(tag, existing_tags, max_distance=1):
    """在已有标签中查找拼写相似的标签。

    仅对英文字母标签进行相似度检测，避免误合并中文标签。
    使用 Damerau-Levenshtein 距离，可以检测相邻字符交换等常见拼写错误。

    Args:
        tag: 要查找的标签（已规范化）
        existing_tags: 已有标签的字典 {normalized_key: display_name}
        max_distance: 最大允许的编辑距离（默认1，即允许1个字符差异或1次相邻交换）

    Returns:
        找到的相似标签的键，如果没有找到则返回 None
    """
    # 只对较长的英文字母标签进行相似度检测
    # 短标签（<4字符）不检测，避免误合并（如 "fps" vs "ps"）
    if len(tag) < 4:
        return None

    # 只对纯英文字母标签进行检测（可以包含空格）
    if not all(c.isalpha() or c.isspace() for c in tag):
        return None

    for existing_key in existing_tags:
        # 长度差距太大，直接跳过
        if abs(len(tag) - len(existing_key)) > max_distance:
            continue

        # 只和同样是英文字母的标签比较
        if not all(c.isalpha() or c.isspace() for c in existing_key):
            continue

        # 使用 Damerau-Levenshtein 距离，可以识别相邻字符交换
        distance = damerau_levenshtein_distance(tag, existing_key)
        if distance <= max_distance:
            return existing_key

    return None


def merge_tags(existing_tags, new_tags, tag_names_dict, enable_fuzzy_match=True):
    """合并标签列表，避免重复。

    Args:
        existing_tags: 已有的标签列表
        new_tags: 新增的标签列表
        tag_names_dict: 标签名称字典 {normalized_key: canonical_name}
        enable_fuzzy_match: 是否启用模糊匹配（默认True）

    Returns:
        合并后的标签字典 {normalized_key: display_name}
    """
    merged = {}

    # 先处理已有标签
    for tag in existing_tags:
        if not isinstance(tag, str) or not tag.strip():
            continue
        key = normalize_tag_key(tag)
        if key:
            merged.setdefault(key, tag.strip())

    # 再处理新标签，使用 tag_names_dict 中的规范名称
    for tag in new_tags:
        if not isinstance(tag, str) or not tag.strip():
            continue
        tag = tag.strip()
        key = normalize_tag_key(tag)
        if not key:
            continue

        # 如果已经存在完全匹配，跳过
        if key in merged:
            continue

        # 尝试模糊匹配：在已合并的标签中查找相似的
        if enable_fuzzy_match:
            similar_key = find_similar_tag(key, merged)
            if similar_key:
                # 找到了相似标签，使用已有的键，不添加新的
                continue

            # 也检查 tag_names_dict 中是否有相似的
            similar_key = find_similar_tag(key, tag_names_dict)
            if similar_key:
                # 使用 tag_names_dict 中的规范名称
                merged[similar_key] = tag_names_dict[similar_key]
                continue

        # 没有找到相似的，添加新标签
        # 优先使用 tag_names_dict 中的规范名称（来自NeoDB）
        canonical = tag_names_dict.get(key, tag)
        merged[key] = canonical

    return merged
