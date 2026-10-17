#!/usr/bin/env python3
"""
知识图谱管道 — 主入口脚本
===========================
手动触发，增量更新。
每次运行扫描 vault 中的 .md 文件，对比时间戳和内容哈希，
对新增/修改的文件进行实体提取并更新知识图谱。

用法:
  python3 kg-pipeline/pipeline.py            # 正常运行
  python3 kg-pipeline/pipeline.py --dry-run  # 预览模式（不写回文件）
  python3 kg-pipeline/pipeline.py --force    # 强制全量重建
"""
import os
import sys
import json
import hashlib
import argparse
import yaml
from datetime import date, datetime
from typing import Dict, List, Set, Any, Optional, Tuple
from pathlib import Path
from collections import defaultdict

# ============================================================
# 路径设置：确保能够 import 管道模块
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KG_ROOT = os.path.dirname(SCRIPT_DIR)  # 管道代码所在的项目根目录
DATA_DIR = os.path.join(KG_ROOT, 'kg-pipeline-data')

# 将管道目录与项目根目录加入 sys.path
# （项目根目录用于 import kg_pipeline_config，与 check_changes.py 共用配置与扫描逻辑）
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
if KG_ROOT not in sys.path:
    sys.path.insert(0, KG_ROOT)

from kg_pipeline_config import scan_vault_md_files  # noqa: E402

from extractors.frontmatter_extractor import (
    parse_frontmatter, extract_tags, extract_aliases,
    extract_related, enrich_frontmatter
)
from extractors.wikilink_extractor import extract_wikilinks
from extractors.inline_tag_extractor import extract_inline_tags, extract_inline_tags_with_slide
from extractors.heading_extractor import extract_headings, get_heading_path
from extractors.outline_parser import extract_outline, extract_arrow_paths
from graph.graph_model import (
    KnowledgeGraph, make_doc_id, make_tag_id,
    make_entity_id, make_heading_id
)
from graph.cooccurrence import (
    build_cooccurrence_matrix, compute_document_similarities,
    compute_tag_coccurrence_weights, get_tag_doc_counts
)
from graph.entity_resolver import EntityResolver
from output.frontmatter_writer import write_enriched_file, read_file_content
from output.wikilink_injector import auto_inject_wikilinks, build_wikilink_recommendations
from output.json_export import export_graph, export_stats_report


# ============================================================
# 配置加载
# ============================================================

def load_config() -> Dict[str, Any]:
    """加载并合并 config.yaml + config.aliases.local.yaml + config.local.yaml 配置。"""
    config_path = os.path.join(SCRIPT_DIR, 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    # 合并本地别名（不提交到 Git）
    aliases_path = os.path.join(KG_ROOT, 'config.aliases.local.yaml')
    if os.path.exists(aliases_path):
        with open(aliases_path, 'r', encoding='utf-8') as f:
            auto = yaml.safe_load(f) or {}
        auto_aliases = auto.get('entity_aliases', {})
        if auto_aliases:
            base = config.get('entity_aliases', {})
            for canonical, aliases in auto_aliases.items():
                if canonical in base:
                    base[canonical] = sorted(set(base[canonical] + aliases))
                else:
                    base[canonical] = aliases
            config['entity_aliases'] = base

    # 合并本地配置（vault_path 等）
    local_path = os.path.join(KG_ROOT, 'config.local.yaml')
    if os.path.exists(local_path):
        with open(local_path, 'r', encoding='utf-8') as f:
            local = yaml.safe_load(f) or {}
        config.update({k: v for k, v in local.items() if v is not None})

    # 环境变量覆盖
    if os.environ.get('KG_VAULT_PATH'):
        config['vault_path'] = os.environ['KG_VAULT_PATH']

    return config


# ============================================================
# 状态管理
# ============================================================

def load_state() -> Dict[str, Any]:
    """加载 index_state.json。损坏时给出可操作的提示而不是裸 traceback。"""
    state_path = os.path.join(DATA_DIR, 'index_state.json')
    if not os.path.exists(state_path):
        return {}

    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"index_state.json 解析失败: {state_path}\n"
            f"  {e}\n"
            f"  该文件记录了每篇文档的提取结果，建图依赖它。\n"
            f"  若确已损坏，从备份恢复，或删除该文件后重跑（会全量重新解析）。"
        ) from e


def save_state(state: Dict[str, Any]) -> None:
    """
    保存 index_state.json（原子写）。

    先写临时文件再 os.replace，避免序列化中途出错或进程被中断时
    留下截断的 JSON —— 那会让下一轮 load_state() 直接崩溃，
    而且要手工从备份恢复。index_state.json 现在承载着建图所需的
    提取结果，损坏的代价比过去大得多。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    state_path = os.path.join(DATA_DIR, 'index_state.json')
    tmp_path = state_path + '.tmp'

    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, state_path)


def compute_file_hash(file_path: str) -> str:
    """计算文件的 MD5 哈希值。"""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


# ============================================================
# 提取结果的持久化
#
# index_state.json 的每条记录同时承载「文件有没有变」和「这篇的提取结果」，
# 建图时用全部文档的提取结果，因此未变更文件不必重新解析、图谱也不会被
# 增量截断。两者放在同一条记录里，不可能不同步。
#
# 不存 content 正文：build_graph_from_extractions 不使用它，只有回写时才需要，
# 而回写只发生在本轮已解析过的变更文件上。
# ============================================================

# 提取器输出结构变化时递增，使 state 里的旧结构失效并触发重新解析
EXTRACTION_SCHEMA_VERSION = 1

_STATE_EXCLUDED_FIELDS = ('content',)


def _json_safe(value: Any) -> Any:
    """
    递归转成可 JSON 序列化的形式。

    python-frontmatter 会把 YAML 里的 `created: 2026-08-04` 解析成 datetime.date，
    直接 json.dump 会抛 TypeError。日期统一转成 ISO 字符串。
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def extraction_to_state(ext: Dict[str, Any]) -> Dict[str, Any]:
    """把单篇提取结果转成可持久化的形式（去掉正文，带上 schema 版本）。"""
    payload = {k: v for k, v in ext.items() if k not in _STATE_EXCLUDED_FIELDS}
    payload['schema_version'] = EXTRACTION_SCHEMA_VERSION
    return _json_safe(payload)


def state_to_extraction(payload: Any) -> Optional[Dict[str, Any]]:
    """
    从 state 记录还原提取结果。

    结构缺失或版本过期时返回 None —— 调用方据此判定该文件需要重新解析。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get('schema_version') != EXTRACTION_SCHEMA_VERSION:
        return None
    return {k: v for k, v in payload.items() if k != 'schema_version'}


# ============================================================
# 文件扫描
# ============================================================

# should_exclude / scan_vault_files 已上移到 kg_pipeline_config，
# 与 check_changes.py 共用同一份实现，避免两边扫出的文件集不一致。
scan_vault_files = scan_vault_md_files


# ============================================================
# 差异检测
# ============================================================

def detect_changes(
    files: Dict[str, str],
    state: Dict[str, Any]
) -> Tuple[List[str], List[str], List[str]]:
    """
    对比文件列表和状态，检测变更。
    返回: (new_files, modified_files, unchanged_files)
    """
    new_files = []
    modified_files = []
    unchanged_files = []

    for rel_path, abs_path in files.items():
        mtime = os.path.getmtime(abs_path)
        file_hash = compute_file_hash(abs_path)

        if rel_path not in state:
            new_files.append(rel_path)
        else:
            stored = state[rel_path]
            if (mtime > stored.get('mtime', 0) and
                    file_hash != stored.get('hash', '')):
                modified_files.append(rel_path)
            else:
                unchanged_files.append(rel_path)

    return new_files, modified_files, unchanged_files


# ============================================================
# 数据提取（单文件）
# ============================================================

def extract_from_file(
    file_path: str,
    rel_path: str,
    resolver: EntityResolver
) -> Dict[str, Any]:
    """
    从单个 .md 文件中提取所有结构化数据。
    返回提取结果字典。
    """
    content = read_file_content(file_path)

    # 1. Frontmatter 提取
    metadata = parse_frontmatter(file_path)
    fm_tags = extract_tags(metadata)
    fm_aliases = extract_aliases(metadata)
    fm_related = extract_related(metadata)

    # 2. Wiki-link 提取
    wikilinks = extract_wikilinks(content)

    # 3. 内联标签提取（🏷️ 标签: 块）
    inline_tags = extract_inline_tags(content)
    inline_tags_with_slide = extract_inline_tags_with_slide(content)

    # 4. 标题提取
    headings = extract_headings(content)

    # 5. 大纲解析
    outlines = extract_outline(content)
    arrow_paths = extract_arrow_paths(content)

    # 6. 合并所有标签（frontmatter + inline）
    all_tags_raw = list(set(fm_tags + inline_tags))
    # 实体归一化
    all_tags_resolved = resolver.resolve_batch(all_tags_raw)
    # 去重
    all_tags_unique = sorted(set(all_tags_resolved))

    return {
        'rel_path': rel_path,
        'content': content,
        'metadata': metadata,
        'fm_tags': fm_tags,
        'fm_aliases': fm_aliases,
        'fm_related': fm_related,
        'wikilinks': wikilinks,
        'inline_tags': inline_tags,
        'inline_tags_with_slide': inline_tags_with_slide,
        'all_tags': all_tags_unique,
        'headings': headings,
        'outlines': outlines,
        'arrow_paths': arrow_paths,
    }


# ============================================================
# 图构建
# ============================================================

def build_graph_from_extractions(
    kg: KnowledgeGraph,
    extractions: Dict[str, Dict[str, Any]],
    resolver: EntityResolver
) -> None:
    """
    将所有文件的提取结果构建为知识图谱。
    """
    # 收集标签-文档映射（用于共现计算）
    doc_tags: Dict[str, List[str]] = {}

    for rel_path, ext in extractions.items():
        doc_id = make_doc_id(rel_path)
        doc_tags[rel_path] = ext['all_tags']

        # --- 文档节点 ---
        kg.add_node(doc_id, 'document',
                    path=rel_path,
                    title=ext['metadata'].get('title', rel_path),
                    aliases=ext['fm_aliases'],
                    provenance=[{
                        'file': rel_path,
                        'form': 'document',
                    }])

        # --- 标签节点 + contains 边 ---
        for tag in ext['fm_tags']:
            tag_id = make_tag_id(tag)
            kg.add_node(tag_id, 'tag',
                        name=tag,
                        provenance=[{
                            'file': rel_path,
                            'form': 'tag',
                        }])
            kg.add_edge(doc_id, tag_id, 'contains', weight=1.0,
                        provenance=[{'file': rel_path, 'form': 'tag'}])

        # --- 实体节点 + references 边 ---
        for entity_name in ext['all_tags']:
            entity_id = make_entity_id(entity_name)
            # 计算该实体在本文件中的出现次数
            inline_count = sum(
                1 for item in ext['inline_tags_with_slide']
                if resolver.resolve(item['tag']) == entity_name
            )
            fm_count = ext['fm_tags'].count(entity_name)
            total_occurrences = inline_count + fm_count

            # 收集相关幻灯片
            slides = sorted(set(
                item['slide'] for item in ext['inline_tags_with_slide']
                if resolver.resolve(item['tag']) == entity_name
            ))

            kg.add_node(entity_id, 'entity',
                        canonical_name=entity_name,
                        provenance=[{
                            'file': rel_path,
                            'form': 'tag',
                            'occurrences': total_occurrences,
                            'slides': slides,
                        }])
            kg.add_edge(doc_id, entity_id, 'references', weight=1.0,
                        provenance=[{
                            'file': rel_path,
                            'form': 'tag',
                            'occurrences': total_occurrences,
                            'slides': slides,
                        }])

        # --- 标题节点 + hierarchy_child 边 ---
        for heading in ext['headings']:
            heading_id = make_heading_id(rel_path, heading['title'])
            heading_path = get_heading_path(ext['headings'], heading['line'])

            kg.add_node(heading_id, 'heading',
                        title=heading['title'],
                        level=heading['level'],
                        line=heading['line'],
                        path=heading_path,
                        provenance=[{
                            'file': rel_path,
                            'form': 'heading',
                        }])
            kg.add_edge(doc_id, heading_id, 'contains', weight=1.0,
                        provenance=[{'file': rel_path, 'form': 'heading'}])

        # --- 大纲路径节点（仅当路径片段匹配已知实体时才创建）---
        known_entity_names = set()
        for tag_list in doc_tags.values():
            known_entity_names.update(tag_list)
        # 也从 fm_tags 和 inline_tags 中收集
        known_entity_names.update(ext['fm_tags'])
        known_entity_names.update(ext['inline_tags'])

        for arrow_path in ext['arrow_paths']:
            path_str = ' → '.join(arrow_path['path'])
            for item in arrow_path['path']:
                resolved = resolver.resolve(item)
                # 仅当该片段匹配已知实体或别名时才创建节点
                if resolved not in known_entity_names and item not in known_entity_names:
                    continue
                entity_id = make_entity_id(resolved)
                if not kg.has_node(entity_id):
                    kg.add_node(entity_id, 'entity',
                                canonical_name=resolved,
                                provenance=[{
                                    'file': rel_path,
                                    'form': 'outline',
                                    'path': path_str,
                                }])
                kg.add_edge(make_doc_id(rel_path), entity_id, 'references', weight=0.5,
                            provenance=[{
                                'file': rel_path,
                                'form': 'outline',
                                'path': path_str,
                            }])

        # --- related 显式关联边 ---
        for related_target in ext['fm_related']:
            # 在已有文档中查找匹配
            for other_rel_path in extractions:
                other_name = other_rel_path.rsplit('.', 1)[0].rsplit('/', 1)[-1]
                if other_name == related_target or other_rel_path == related_target:
                    target_doc_id = make_doc_id(other_rel_path)
                    kg.add_edge(doc_id, target_doc_id, 'related_to', weight=1.0,
                                provenance=[{
                                    'file': rel_path,
                                    'form': 'wikilink',
                                }])
                    break

    # --- 标签共现边 ---
    cooccur_matrix = build_cooccurrence_matrix(doc_tags)
    tag_doc_counts = get_tag_doc_counts(doc_tags)
    cooccur_weights = compute_tag_coccurrence_weights(cooccur_matrix, tag_doc_counts)

    for (tag_a, tag_b), weight in cooccur_weights.items():
        entity_a = resolver.resolve(tag_a)
        entity_b = resolver.resolve(tag_b)
        entity_id_a = make_entity_id(entity_a)
        entity_id_b = make_entity_id(entity_b)

        if entity_id_a == entity_id_b:
            continue

        # 收集共现来源
        cooccur_provenance = []
        shared_docs = []
        for rel_path, tags in doc_tags.items():
            resolved_tags = set(resolver.resolve(t) for t in tags)
            if entity_a in resolved_tags and entity_b in resolved_tags:
                shared_docs.append(rel_path)

        for doc in shared_docs:
            cooccur_provenance.append({
                'file': doc,
                'form': 'coocurrence',
            })

        kg.add_edge(entity_id_a, entity_id_b, 'cooccurs_with',
                    weight=weight,
                    provenance=cooccur_provenance)

    # --- 文档相似度边 ---
    doc_similarities = compute_document_similarities(doc_tags)
    for sim in doc_similarities:
        doc_id_a = make_doc_id(sim['doc_a'])
        doc_id_b = make_doc_id(sim['doc_b'])
        kg.add_edge(doc_id_a, doc_id_b, 'similar_to',
                    weight=sim['similarity'],
                    provenance=[{
                        'file': sim['doc_a'],
                        'form': 'similarity',
                        'shared_tags': sim['shared_tags'],
                    }, {
                        'file': sim['doc_b'],
                        'form': 'similarity',
                        'shared_tags': sim['shared_tags'],
                    }])


# ============================================================
# 别名自动发现
# ============================================================

def _has_chinese(text: str) -> bool:
    """检测文本是否包含中文字符。"""
    for ch in text:
        if '一' <= ch <= '鿿' or '㐀' <= ch <= '䶿':
            return True
    return False


def discover_aliases(
    extractions: Dict[str, Dict[str, Any]],
    resolver: EntityResolver,
    kg: KnowledgeGraph
) -> Dict[str, List[str]]:
    """
    从高精度信号自动发现中英双语别名候选。
    原则：宁可漏掉，不可混入噪声。所有候选均基于字符串层面的强匹配。

    信号1: 英文词恰好是中文实体名的子串 (如 "AIOps" ⊂ "AIOps演进")
    信号2: 归一化后精确匹配 (英文 lower+去空格 == 中文对应)
    """
    candidates: Dict[str, set] = defaultdict(set)

    all_entities = set()
    for ext in extractions.values():
        all_entities.update(ext['all_tags'])
    zh_entities = {e for e in all_entities if _has_chinese(e)}
    en_entities = {e for e in all_entities if not _has_chinese(e) and e.isascii() and len(e) >= 3}

    for zh in zh_entities:
        canonical = resolver.resolve(zh)
        if canonical != zh:  # 已有映射，跳过
            continue

        zh_lower = zh.lower()
        for en in en_entities:
            en_lower = en.lower()

            # 信号1: 英文字串包含在中文实体中 (如 "KPI" ⊂ "KPI矩阵")
            if len(en) >= 3 and en_lower in zh_lower:
                candidates[zh].add(en)

            # 信号2: 归一化精确匹配 (如 "agenticai" == "agenticai")
            zh_normalized = zh_lower.replace(' ', '').replace('-', '').replace('_', '')
            en_normalized = en_lower.replace(' ', '').replace('-', '').replace('_', '')
            if len(en_normalized) >= 4 and zh_normalized == en_normalized:
                candidates[zh].add(en)

    # 过滤：别名数 >5 的通常是泛化词，仅保留前5个
    result = {}
    for zh, en_set in candidates.items():
        en_list = sorted(en_set)
        if len(en_list) > 5:
            en_list = en_list[:5]
        if en_list:
            result[zh] = en_list

    return result


def save_discovered_aliases(
    discovered: Dict[str, List[str]],
    output_path: str
) -> None:
    """将发现的别名写入 config.aliases.local.yaml（保留已有条目，合并新增）。"""
    existing = {}
    if os.path.exists(output_path):
        with open(output_path, 'r') as f:
            data = yaml.safe_load(f) or {}
            existing = data.get('entity_aliases', {})

    # 合并：已有 + 新发现
    merged = dict(existing)
    for canonical, aliases in discovered.items():
        if canonical in merged:
            merged[canonical] = sorted(set(merged[canonical] + aliases))
        else:
            merged[canonical] = aliases

    # 写入 YAML
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write('# config.aliases.local.yaml — 实体别名映射（本地，不提交到 Git）\n')
        f.write('# 手动维护 + 管道自动发现合并，与 config.yaml 分开加载\n')
        f.write('\n')
        yaml.dump({'entity_aliases': merged}, f, allow_unicode=True, default_flow_style=False)


# ============================================================
# 回写操作
# ============================================================

def write_back_enrichments(
    extractions: Dict[str, Dict[str, Any]],
    kg: KnowledgeGraph,
    doc_similarities: List[Dict],
    config: Dict[str, Any],
    vault_path: str,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    回写 enriched frontmatter 和 wiki-links 到变更文件。
    返回回写统计。
    """
    create_backup = config.get('create_backup', True)
    auto_threshold = config.get('auto_inject_threshold', 0.5)
    suggest_threshold = config.get('suggest_threshold', 0.3)
    exclude_tags = set(config.get('exclude_tags', []))

    stats = {
        'frontmatter_writes': 0,
        'wikilink_injections': 0,
        'suggestions': {},
    }

    for rel_path, ext in extractions.items():
        abs_path = os.path.join(vault_path, rel_path)
        content = ext['content']

        # 1. 生成 enriched frontmatter
        # 从图谱中收集该文档关联的所有实体作为潜在标签
        doc_id = make_doc_id(rel_path)
        doc_entities = []
        for u, v, attrs in kg.graph.edges(data=True):
            if attrs.get('type') in ('references', 'contains') and attrs.get('weight', 0) >= 0.5:
                if u == doc_id and v.startswith('entity:'):
                    doc_entities.append(v.replace('entity:', '', 1))
                elif v == doc_id and u.startswith('entity:'):
                    doc_entities.append(u.replace('entity:', '', 1))

        # 过滤掉禁止作为标签的词（子串匹配、不区分大小写）。
        # 管道是从**正文**派生标签的，而「封面/目录/附录/分隔页」这类结构性词汇
        # 与公司名会从正文（页脚、"About <公司>" 章节）渗进来并沉淀到 tags 里。
        # 上游 ppt-extract 有同样的禁用词表，但回写发生在上游之后，挡不住这里。
        #
        # 用子串而非精确匹配：这类词会组合出现（`Accenture内部`、`章节分隔页`），
        # 精确匹配会漏。见 config.yaml 的 exclude_tags。
        if exclude_tags:
            lowered = [w.lower() for w in exclude_tags]
            doc_entities = [t for t in doc_entities
                            if not any(w in t.lower() for w in lowered)]

        enriched = enrich_frontmatter(
            ext['metadata'],
            new_tags=sorted(set(doc_entities)),
        )

        if dry_run:
            if doc_entities:
                print(f"  [DRY-RUN] 将为 {rel_path} 添加 tags: {doc_entities[:5]}...")
                stats['frontmatter_writes'] += 1
        else:
            changed = write_enriched_file(abs_path, enriched, create_backup)
            if changed:
                stats['frontmatter_writes'] += 1
                print(f"  ✓ 回写 frontmatter: {rel_path}")

        # 2. Wiki-link 建议
        recs = build_wikilink_recommendations(
            doc_similarities, rel_path, auto_threshold, suggest_threshold
        )
        if recs['auto_inject'] or recs['suggest']:
            stats['suggestions'][rel_path] = recs

        # 3. 自动注入 wiki-links
        if recs['auto_inject']:
            if dry_run:
                print(f"  [DRY-RUN] 将为 {rel_path} 自动注入 wiki-links: "
                      f"{[r[0] for r in recs['auto_inject']]}")
                stats['wikilink_injections'] += 1
            else:
                new_content, changed = auto_inject_wikilinks(
                    abs_path, content, recs['auto_inject'],
                    threshold=auto_threshold,
                    create_backup=create_backup
                )
                if changed:
                    # 需要回写到文件
                    from output.frontmatter_writer import write_file_content
                    write_file_content(abs_path, new_content)
                    stats['wikilink_injections'] += 1
                    print(f"  ✓ 注入 wiki-links: {rel_path} "
                          f"→ {[r[0] for r in recs['auto_inject']]}")

    return stats


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='知识图谱管道 — 增量更新')
    parser.add_argument('--dry-run', action='store_true',
                        help='预览模式，不实际修改文件')
    parser.add_argument('--force', action='store_true',
                        help='强制全量重建（忽略增量状态）')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='详细输出')
    args = parser.parse_args()

    print('=' * 60)
    print('  知识图谱管道 (Knowledge Graph Pipeline)')
    print('=' * 60)

    # 1. 加载配置
    print('\n[1/9] 加载配置...')
    config = load_config()
    vault_path = config.get('vault_path', '')
    if not vault_path or not os.path.isdir(vault_path):
        print(f'  错误: vault_path 无效: {vault_path}')
        sys.exit(1)
    exclude_dirs = config.get('exclude_dirs', [])
    exclude_patterns = config.get('exclude_patterns', [])
    print(f'  Vault 路径: {vault_path}')
    print(f'  排除目录: {exclude_dirs}')
    print(f'  排除模式: {exclude_patterns}')

    # 2. 加载状态
    print('\n[2/9] 加载索引状态...')
    state = load_state() if not args.force else {}
    print(f'  已记录文件数: {len(state)}')
    if args.force:
        print('  ⚠ --force 模式，忽略增量状态，全量重建')

    # 3. 扫描文件
    print('\n[3/9] 扫描 vault 中的 .md 文件...')
    files = scan_vault_files(vault_path, exclude_dirs, exclude_patterns)
    print(f'  发现 {len(files)} 个 .md 文件:')
    for rel_path in sorted(files.keys()):
        print(f'    - {rel_path}')

    # 4. 差异检测
    print('\n[4/9] 检测文件变更...')
    if args.force:
        new_files = sorted(files.keys())
        modified_files = []
        unchanged_files = []
    else:
        new_files, modified_files, unchanged_files = detect_changes(files, state)

    print(f'  新文件:   {len(new_files)}')
    for f in new_files:
        print(f'    + {f}')
    print(f'  已修改:   {len(modified_files)}')
    for f in modified_files:
        print(f'    ~ {f}')
    print(f'  未变化:   {len(unchanged_files)}')

    # 需要处理的文件（真正发生变更的）
    files_to_process = new_files + modified_files

    # 未变更的文件本可复用 state 里的提取结果，但下面两种情况必须重新解析：
    #   - 记录里没有 extraction（启用本机制之前的存量记录）
    #   - extraction 结构版本过期（提取器改过）
    # 这类文件的 hash 没变，所以不会被 detect_changes 判为变更；必须在这里补判，
    # 否则它们会悄无声息地掉出图谱 —— 这正是图谱被增量截断的表现。
    # 注意：它们只参与建图，不回写（未变更就不该改写文件）。
    stale_extraction = [
        rel_path for rel_path in unchanged_files
        if state_to_extraction(state.get(rel_path, {}).get('extraction')) is None
    ]

    # 本轮真正需要解析的文件 = 变更的 + 缓存失效的
    files_to_parse = files_to_process + stale_extraction

    # 已删除的文件：state 里有记录、vault 里已不存在。
    # detect_changes 只对比「存在的文件」，看不出删除，必须在这里单独判定。
    # 删除也算「有活要干」——否则只剩删除时会提前返回，图里仍留着已删文档的节点，
    # 且 state 永远清不掉，check_changes 会一直报「已删除」并退出码 1。
    deleted_files = [rel_path for rel_path in state if rel_path not in files]
    if deleted_files:
        print(f'  已删除:   {len(deleted_files)}')
        for rel_path in deleted_files:
            print(f'    - {rel_path}')

    if not files_to_parse and not deleted_files:
        print('\n  ✅ 无文件变更，跳过处理。')
        return

    if stale_extraction:
        print(f'  提取缓存待补建: {len(stale_extraction)} 个文件'
              f'（未变更，仅参与建图，不回写）')

    # 5. 实体解析器初始化
    print('\n[5/9] 初始化实体解析器...')
    entity_aliases = config.get('entity_aliases', {})
    resolver = EntityResolver(entity_aliases)
    print(f'  规范实体数: {len(resolver.get_canonical_names())}')
    print(f'  别名词条数: {len(resolver._alias_index)}')

    # 6. 提取数据（仅未命中的文件真正读盘解析）
    print(f'\n[6/9] 提取结构化数据（解析 {len(files_to_parse)} 个文件，'
          f'其余 {len(files) - len(files_to_parse)} 个复用缓存）...')
    extractions = {}
    for rel_path in files_to_parse:
        abs_path = files[rel_path]
        print(f'  处理: {rel_path}')
        try:
            ext = extract_from_file(abs_path, rel_path, resolver)
            extractions[rel_path] = ext
            print(f'    └ tags={len(ext["all_tags"])}, '
                  f'headings={len(ext["headings"])}, '
                  f'wikilinks={len(ext["wikilinks"])}, '
                  f'inline_tags={len(ext["inline_tags"])}')
        except Exception as e:
            print(f'    ✗ 错误: {e}')
            if args.verbose:
                import traceback
                traceback.print_exc()

    # 6b. 未变更文件：从 state 还原提取结果，不重新打开文件。
    # 图谱必须由「全部文档」构建：fm_related 的目标解析要遍历所有文档，
    # 标签共现权重、文档相似度本身也是全局统计量。只喂变更文件会得到残缺的图。
    reused = 0
    for rel_path in unchanged_files:
        if rel_path in extractions:
            continue
        cached = state_to_extraction(state.get(rel_path, {}).get('extraction'))
        if cached is not None:
            extractions[rel_path] = cached
            reused += 1
    print(f'  └ 复用缓存: {reused} 个文件')

    # 只有发生变更的文件才回写（未变更的不改写、不产生新 .bak）
    changed_extractions = {
        rel_path: ext for rel_path, ext in extractions.items()
        if rel_path in files_to_process
    }

    # 7. 构建/更新图
    print('\n[7/9] 构建知识图谱...')
    kg = KnowledgeGraph()
    build_graph_from_extractions(kg, extractions, resolver)

    stats = kg.stats()
    print(f'  节点: {stats["total_nodes"]}')
    for nt, count in sorted(stats['node_types'].items()):
        print(f'    {nt}: {count}')
    print(f'  边:   {stats["total_edges"]}')
    for et, count in sorted(stats['edge_types'].items()):
        print(f'    {et}: {count}')

    # 7b. 别名自动发现
    if not args.dry_run:
        print('\n[7b/9] 别名自动发现...')
        discovered = discover_aliases(extractions, resolver, kg)
        if discovered:
            aliases_path = os.path.join(KG_ROOT, 'config.aliases.local.yaml')
            save_discovered_aliases(discovered, aliases_path)
            print(f'  ✓ 发现 {len(discovered)} 组候选别名 → {aliases_path}')
        else:
            print('  - 未发现新的候选别名')

    # 8. 回写
    print('\n[8/9] 回写 enriched data...')
    if args.dry_run:
        print('  ⚠ DRY-RUN 模式：仅预览，不实际修改文件')

    # 相似度用全集算（wiki-link 推荐才是完整的）
    doc_similarities = compute_document_similarities(
        {rel_path: ext['all_tags'] for rel_path, ext in extractions.items()}
    )

    # 回写只针对变更文件：未变更文件既不该被改写，也不该产生新 .bak
    writeback_stats = write_back_enrichments(
        changed_extractions, kg, doc_similarities, config, vault_path,
        dry_run=args.dry_run
    )

    if args.dry_run:
        print(f'\n  DRY-RUN 预览:')
        print(f'    将回写 frontmatter: {writeback_stats["frontmatter_writes"]} 个文件')
        print(f'    将注入 wiki-links:  {writeback_stats["wikilink_injections"]} 个文件')
        for rel_path, recs in writeback_stats['suggestions'].items():
            if recs['suggest']:
                print(f'    建议审核: {rel_path} → {[r[0] for r in recs["suggest"]]}')

    # 9. 导出 + 更新状态
    if not args.dry_run:
        print('\n[9/9] 导出图数据 + 更新状态...')

        # 导出 graph.json
        graph_dict = kg.to_dict()
        output_path = os.path.join(DATA_DIR, 'graph.json')
        export_graph(graph_dict, output_path)
        print(f'  ✓ 导出 graph.json → {output_path}')

        # 更新 index_state.json —— 未变更条目的 mtime/hash/extraction 原样保留，
        # 只刷新本轮解析过的（变更的 + 缓存失效的），并附上提取结果供下轮复用。
        new_state = dict(state)
        for rel_path in files_to_parse:
            if rel_path not in extractions:
                continue  # 该文件解析失败，保留旧记录
            abs_path = files[rel_path]
            new_state[rel_path] = {
                'mtime': os.path.getmtime(abs_path),
                'hash': compute_file_hash(abs_path),
                'extraction': extraction_to_state(extractions[rel_path]),
            }

        # 清理已删除文件的记录。否则 check_changes.py 的 `deleted_files` 永远非空，
        # 每次都报「有变更」并退出码 1，而管道已无文件可处理。
        removed = [rel_path for rel_path in new_state if rel_path not in files]
        for rel_path in removed:
            del new_state[rel_path]

        save_state(new_state)
        print(f'  ✓ 更新 index_state.json ({len(new_state)} 条记录'
              f'{f"，清理已删除 {len(removed)} 条" if removed else ""})')

        # 统计报告
        report = export_stats_report(stats, graph_dict)
        print('\n' + report)
    else:
        # dry-run 也打印统计
        print(f'\n  未匹配实体（需人工审核）: {len(resolver.get_unmatched())}')
        if resolver.get_unmatched():
            for u in resolver.get_unmatched()[:10]:
                print(f'    - {u}')

    print('\n✅ 管道运行完成。')


if __name__ == '__main__':
    main()
