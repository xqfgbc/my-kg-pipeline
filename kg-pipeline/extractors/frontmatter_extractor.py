"""
前端元数据提取器
解析和生成 YAML frontmatter，处理 tags、aliases、related 等字段。
"""
import frontmatter
import re
from typing import Optional, Dict, List, Any


def parse_frontmatter(file_path: str) -> Dict[str, Any]:
    """解析 .md 文件的 frontmatter，返回字典。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            post = frontmatter.load(f)
        return dict(post.metadata)
    except Exception:
        return {}


def extract_tags(metadata: Dict[str, Any]) -> List[str]:
    """从 frontmatter metadata 中提取 tags 列表。"""
    tags = metadata.get('tags', [])
    if isinstance(tags, str):
        tags = [tags]
    return [t.strip() for t in tags if t and isinstance(t, str)]


def extract_aliases(metadata: Dict[str, Any]) -> List[str]:
    """从 frontmatter metadata 中提取 aliases 列表。"""
    aliases = metadata.get('aliases', [])
    if isinstance(aliases, str):
        aliases = [aliases]
    return [a.strip() for a in aliases if a and isinstance(a, str)]


def extract_related(metadata: Dict[str, Any]) -> List[str]:
    """
    从 frontmatter metadata 中提取 related 字段。
    返回清理后的 [[wikilink]] 名称列表。
    """
    related = metadata.get('related', [])
    if isinstance(related, str):
        related = [related]
    result = []
    for item in related:
        if not isinstance(item, str):
            continue
        # 提取 [[...]] 中的内容
        match = re.match(r'^\[\[(.+?)\]\]$', item.strip())
        if match:
            result.append(match.group(1).strip())
        else:
            result.append(item.strip())
    return result


def build_frontmatter(metadata: Dict[str, Any]) -> str:
    """
    将 metadata 字典渲染为 YAML frontmatter 字符串。
    格式化为适合 .md 文件顶部嵌入的字符串。
    """
    if not metadata:
        return ''
    # 使用 frontmatter 库的 dumps 功能
    post = frontmatter.Post('', **metadata)
    fm_str = frontmatter.dumps(post)
    # dumps 返回完整的 frontmatter + content，我们只需要 frontmatter 部分
    if fm_str.startswith('---\n'):
        end_idx = fm_str.find('---\n', 4)
        if end_idx != -1:
            return fm_str[:end_idx + 4]
    return fm_str


def enrich_frontmatter(
    metadata: Dict[str, Any],
    new_tags: Optional[List[str]] = None,
    new_aliases: Optional[List[str]] = None,
    new_related: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    在现有 metadata 基础上补充新的 tags/aliases/related，
    去重合并后返回新的 metadata 字典。
    """
    enriched = dict(metadata)

    if new_tags:
        existing = set(extract_tags(enriched))
        enriched['tags'] = sorted(existing | set(new_tags))

    if new_aliases:
        existing = set(extract_aliases(enriched))
        enriched['aliases'] = sorted(existing | set(new_aliases))

    if new_related:
        existing = set(extract_related(enriched))
        enriched['related'] = [f'[[{r}]]' for r in sorted(existing | set(new_related))]

    return enriched
