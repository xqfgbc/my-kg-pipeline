"""
Frontmatter 回写模块
将 enriched frontmatter 写回到 .md 文件中。
"""
import os
import shutil
import re
from typing import Dict, List, Any, Optional


def read_file_content(file_path: str) -> str:
    """读取文件全部内容。"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()


def write_file_content(file_path: str, content: str) -> None:
    """写入文件内容。"""
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)


def create_backup(file_path: str) -> Optional[str]:
    """创建 .bak 备份文件，返回备份路径。"""
    backup_path = file_path + '.bak'
    shutil.copy2(file_path, backup_path)
    return backup_path


def has_frontmatter(content: str) -> bool:
    """检查内容是否以 frontmatter 开头。"""
    return content.lstrip().startswith('---')


def parse_file_parts(content: str) -> tuple:
    """
    将文件内容分割为 (existing_fm_dict, fm_raw_str, body)。
    existing_fm_dict: 从 --- 块解析出的 dict，无 frontmatter 则为 {}
    fm_raw_str: --- 块原始字符串（含 ---）
    body: frontmatter 之后的内容
    """
    stripped = content.lstrip()
    if not stripped.startswith('---'):
        return {}, '', content

    # 找到 frontmatter 结束的 ---
    end_match = re.search(r'\n---\n', stripped[3:])
    if not end_match:
        # 没有结束标记，当作无 frontmatter
        return {}, '', content

    fm_end = 3 + end_match.start() + 4  # 包含结尾的 ---\n
    fm_raw = stripped[:fm_end]
    body = stripped[fm_end:]

    # 简单解析 YAML 为 dict
    fm_dict = _parse_simple_yaml(fm_raw)
    return fm_dict, fm_raw, body


def _parse_simple_yaml(fm_str: str) -> Dict[str, Any]:
    """简单解析 YAML frontmatter 字符串为字典。"""
    import yaml
    try:
        # 去掉首尾的 ---
        inner = fm_str.strip()
        if inner.startswith('---'):
            inner = inner[3:].strip()
        if inner.endswith('---'):
            inner = inner[:-3].strip()
        result = yaml.safe_load(inner)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {}


def render_frontmatter(metadata: Dict[str, Any]) -> str:
    """将 metadata 字典渲染为 frontmatter 字符串。"""
    import yaml
    if not metadata:
        return ''
    yaml_str = yaml.dump(
        metadata,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False
    ).strip()
    return f'---\n{yaml_str}\n---\n'


def write_enriched_file(
    file_path: str,
    enriched_metadata: Dict[str, Any],
    create_backup_flag: bool = True
) -> bool:
    """
    将 enriched frontmatter 写回 .md 文件。
    保留原有 frontmatter 中未涉及的字段，仅更新/添加新字段。
    返回 True 表示成功写入。
    """
    content = read_file_content(file_path)

    if has_frontmatter(content):
        existing_fm, _, body = parse_file_parts(content)
        # 合并: enriched 覆盖 existing
        merged = dict(existing_fm)
        merged.update(enriched_metadata)
        new_content = render_frontmatter(merged) + body
    else:
        # 文件没有 frontmatter，创建新的
        new_content = render_frontmatter(enriched_metadata) + '\n' + content.lstrip()

    if new_content == content:
        return False  # 没有变化

    if create_backup_flag:
        create_backup(file_path)

    write_file_content(file_path, new_content)
    return True
