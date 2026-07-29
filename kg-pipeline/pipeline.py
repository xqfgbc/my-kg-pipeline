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
from datetime import datetime
from typing import Dict, List, Set, Any, Optional, Tuple
from pathlib import Path
from collections import defaultdict

# ============================================================
# 路径设置：确保能够 import 管道模块
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KG_ROOT = os.path.dirname(SCRIPT_DIR)  # 管道代码所在的项目根目录
DATA_DIR = os.path.join(KG_ROOT, 'kg-pipeline-data')

# 将管道目录加入 sys.path
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

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
    """加载 config.yaml 配置文件。"""
    config_path = os.path.join(SCRIPT_DIR, 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================
# 状态管理
# ============================================================

def load_state() -> Dict[str, Any]:
    """加载 index_state.json。"""
    state_path = os.path.join(DATA_DIR, 'index_state.json')
    if os.path.exists(state_path):
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_state(state: Dict[str, Any]) -> None:
    """保存 index_state.json。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    state_path = os.path.join(DATA_DIR, 'index_state.json')
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def compute_file_hash(file_path: str) -> str:
    """计算文件的 MD5 哈希值。"""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


# ============================================================
# 文件扫描
# ============================================================

def should_exclude(
    rel_path: str,
    exclude_dirs: List[str],
    exclude_patterns: List[str]
) -> bool:
    """判断文件是否应该被排除。"""
    import fnmatch

    # 检查目录排除
    parts = rel_path.replace('\\', '/').split('/')
    for d in exclude_dirs:
        if d in parts:
            return True

    # 检查模式排除
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        # 也匹配路径中的任意位置
        normalized = rel_path.replace('\\', '/')
        if fnmatch.fnmatch('/' + normalized, pattern):
            return True

    return False


def scan_vault_files(
    vault_path: str,
    exclude_dirs: List[str],
    exclude_patterns: List[str]
) -> Dict[str, str]:
    """
    扫描 vault 中所有 .md 文件。
    返回 {relative_path: absolute_path}
    """
    files = {}
    for root, dirs, _ in os.walk(vault_path):
        # 过滤目录
        dirs[:] = [d for d in dirs if not d.startswith('.') or d in ['.obsidian']]

        for fname in os.listdir(root):
            if not fname.endswith('.md'):
                continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, vault_path)

            if should_exclude(rel_path, exclude_dirs, exclude_patterns):
                continue

            files[rel_path] = abs_path

    return files


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

    files_to_process = new_files + modified_files
    if not files_to_process:
        print('\n  ✅ 无文件变更，跳过处理。')
        return

    # 5. 实体解析器初始化
    print('\n[5/9] 初始化实体解析器...')
    entity_aliases = config.get('entity_aliases', {})
    resolver = EntityResolver(entity_aliases)
    print(f'  规范实体数: {len(resolver.get_canonical_names())}')
    print(f'  别名词条数: {len(resolver._alias_index)}')

    # 6. 提取数据
    print(f'\n[6/9] 提取结构化数据（{len(files_to_process)} 个文件）...')
    extractions = {}
    for rel_path in files_to_process:
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

    # 8. 回写
    print('\n[8/9] 回写 enriched data...')
    if args.dry_run:
        print('  ⚠ DRY-RUN 模式：仅预览，不实际修改文件')

    doc_similarities = compute_document_similarities(
        {rel_path: ext['all_tags'] for rel_path, ext in extractions.items()}
    )

    writeback_stats = write_back_enrichments(
        extractions, kg, doc_similarities, config, vault_path,
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

        # 更新 index_state.json
        new_state = dict(state)  # 保留旧状态
        for rel_path in files_to_process:
            abs_path = files[rel_path]
            new_state[rel_path] = {
                'mtime': os.path.getmtime(abs_path),
                'hash': compute_file_hash(abs_path),
            }
        save_state(new_state)
        print(f'  ✓ 更新 index_state.json ({len(new_state)} 条记录)')

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
