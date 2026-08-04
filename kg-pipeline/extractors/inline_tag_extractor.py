"""
内联标签提取器
解析 PPT 提取文档特有的 `> 🏷️ 标签:` 块，提取内联标签。
"""
import re
from typing import List, Dict, Set


# 匹配 > 🏷️ 标签: `tag1` `tag2` ...
# 也匹配 > **🏷️ 标签**: `tag1` `tag2` ... (bold format)
# 注意：bold 标记包裹整个 "🏷️ 标签"，即 **🏷️ 标签**
INLINE_TAG_PATTERN = re.compile(
    r'>\s*\*{0,2}🏷️\s*标签\*{0,2}[：:]\s*(.+)',
    re.UNICODE
)

# 匹配 > **标签**: `tag1` `tag2` ... (without emoji)
BOLD_TAG_PATTERN = re.compile(
    r'>\s*\*\*标签\*\*[：:]\s*(.+)',
    re.UNICODE
)

# 匹配 > **Tags**: `tag1` `tag2` ... (English variant)
ENGLISH_TAG_PATTERN = re.compile(
    r'>\s*\*{0,2}Tags?\*{0,2}[：:]\s*(.+)',
    re.UNICODE | re.IGNORECASE
)

# 提取反引号中的标签
BACKTICK_TAG_PATTERN = re.compile(r'`([^`]+)`')


def extract_inline_tags(content: str) -> List[str]:
    """
    从文档正文中提取所有内联标签（🏷️ 标签: 块中的标签）。
    返回去重后的标签列表。
    """
    all_tags: Set[str] = set()

    lines = content.split('\n')
    for line in lines:
        # 匹配 > 🏷️ 标签: `tag1` `tag2` ...
        m = INLINE_TAG_PATTERN.search(line)
        if not m:
            m = BOLD_TAG_PATTERN.search(line)
        if not m:
            m = ENGLISH_TAG_PATTERN.search(line)
        if m:
            tag_text = m.group(1)
            tags = BACKTICK_TAG_PATTERN.findall(tag_text)
            for tag in tags:
                tag = tag.strip()
                if tag:
                    all_tags.add(tag)

    return sorted(all_tags)


def extract_inline_tags_with_slide(content: str) -> List[Dict]:
    """
    提取内联标签，同时关联所在幻灯片编号或最近标题。
    返回 [{'tag': str, 'slide': str}, ...]

    - ppt-extract 文档: slide = "Slide N"（匹配 ## Slide N 标题）
    - web-pdf-extract 等非 Slide 文档: slide = 最近 ## 或 ### 标题文本
    """
    results = []
    lines = content.split('\n')
    current_slide = None

    for i, line in enumerate(lines):
        # 检测当前幻灯片标题（ppt-extract 文档）
        slide_match = re.match(r'^##\s+Slide\s+(\d+)', line, re.IGNORECASE)
        if slide_match:
            current_slide = f"Slide {slide_match.group(1)}"
        else:
            # 回退：检测普通 ##/### 标题（web-pdf-extract 等非 Slide 文档）
            heading_match = re.match(r'^(#{2,3})\s+(.+)', line)
            if heading_match:
                title = heading_match.group(2).strip()
                # 排除元信息标题（摘要、Tags 等）
                if not re.match(r'^(摘要|Tags?|🔗|📋|相关文档|幻灯片目录)', title):
                    current_slide = title

        # 匹配内联标签
        tag_match = INLINE_TAG_PATTERN.search(line)
        if not tag_match:
            tag_match = BOLD_TAG_PATTERN.search(line)
        if not tag_match:
            tag_match = ENGLISH_TAG_PATTERN.search(line)
        if tag_match:
            tag_text = tag_match.group(1)
            tags = BACKTICK_TAG_PATTERN.findall(tag_text)
            for tag in tags:
                tag = tag.strip()
                if tag:
                    results.append({
                        'tag': tag,
                        'slide': current_slide or 'unknown'
                    })

    return results
