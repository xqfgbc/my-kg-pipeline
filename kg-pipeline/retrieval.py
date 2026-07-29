#!/usr/bin/env python3
"""
知识库检索器 —— 根据用户问题从图谱和原文中检索相关知识。
用法: python3 kg-pipeline/retrieval.py "如何实现AIOps"
输出: JSON，包含匹配的实体、关联概念、原文摘要
"""
import sys
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KG_DIR = os.path.dirname(SCRIPT_DIR)
GRAPH_PATH = os.path.join(KG_DIR, "kg-pipeline-data", "graph.json")

# 动态加载本地配置
sys.path.insert(0, KG_DIR)
from kg_pipeline_config import get_vault_path
VAULT_PATH = get_vault_path()


def load_graph() -> Dict:
    with open(GRAPH_PATH, 'r') as f:
        return json.load(f)


def tokenize_query(query: str) -> List[str]:
    """中文+英文混合分词，提取关键词。"""
    # 提取中文词（2-6字连续组合）和英文单词
    tokens = []
    # 英文词
    eng_words = re.findall(r'[A-Za-z]+', query)
    tokens.extend(w.lower() for w in eng_words if len(w) >= 2)

    # 中文词（简单 sliding window）
    chinese_chars = re.findall(r'[一-鿿]+', query)
    for chunk in chinese_chars:
        for wlen in range(2, min(7, len(chunk) + 1)):
            for i in range(len(chunk) - wlen + 1):
                tokens.append(chunk[i:i + wlen])

    # 加入原始查询全文
    tokens.append(query.lower())
    return list(set(tokens))


def match_entities(graph: Dict, tokens: List[str], top_k: int = 10) -> List[Dict]:
    """匹配实体节点，按相关度排序。"""
    entities = [n for n in graph['nodes'] if n['type'] in ('entity', 'tag', 'topic')]
    scored = []

    for e in entities:
        name = (e.get('canonical_name') or e.get('name') or '').lower()
        score = 0
        for token in tokens:
            if token in name:
                score += len(token) / len(name)
            # 别名匹配
            for p in e.get('provenance', []):
                if token in str(p).lower():
                    score += 0.1

        if score > 0:
            scored.append((score, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]


def get_related_entities(graph: Dict, entity_id: str, top_k: int = 8) -> List[Tuple[str, float]]:
    """获取与指定实体共现权重最高的关联实体。"""
    related = []
    for e in graph['edges']:
        if e['type'] != 'cooccurs_with':
            continue
        if e['source'] == entity_id:
            related.append((e['target'].replace('entity:', ''), e['weight']))
        elif e['target'] == entity_id:
            related.append((e['source'].replace('entity:', ''), e['weight']))

    related.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    result = []
    for name, weight in related:
        if name not in seen:
            seen.add(name)
            result.append((name, weight))
            if len(result) >= top_k:
                break
    return result


def extract_passages_from_docs(graph: Dict, entity_ids: List[str], vault: str) -> List[Dict]:
    """
    根据实体 provenance 定位原文段落。
    提取幻灯片摘要和关键正文。
    """
    passages = []
    seen = set()

    # 收集所有 provenance 信息
    target_entities = set(entity_ids)
    for n in graph['nodes']:
        if n['id'] in target_entities:
            for p in n.get('provenance', []):
                doc_path = p['file']
                slides = set(p.get('slides', []))
                for s in slides:
                    key = (doc_path, s)
                    if key not in seen:
                        seen.add(key)
                        passages.append({
                            'doc': doc_path,
                            'slide': s,
                            'entity': n.get('canonical_name', n['id']),
                        })

    # 读取原文，提取摘要和正文
    result = []
    doc_cache = {}
    for psg in passages[:30]:  # 限制数量
        doc_path = psg['doc']
        if doc_path not in doc_cache:
            try:
                with open(os.path.join(vault, doc_path), 'r') as f:
                    doc_cache[doc_path] = f.read()
            except Exception:
                continue

        content = doc_cache[doc_path]
        slide_header = f"## {psg['slide']}"

        # 找到对应幻灯片的位置
        idx = content.find(slide_header)
        if idx == -1:
            continue

        # 提取该幻灯片的内容（到下一个 ## 或文件末尾）
        next_idx = content.find('\n## ', idx + len(slide_header))
        if next_idx == -1:
            slide_content = content[idx:]
        else:
            slide_content = content[idx:next_idx]

        # 提取摘要
        summary_match = re.search(
            r'> \*?\*?📝\s*摘要\*?\*?[：:]\s*(.+?)(?:\n|$)',
            slide_content
        )
        summary = summary_match.group(1) if summary_match else ''

        # 提取标题
        title_match = re.search(r'^## .+$', slide_content, re.MULTILINE)
        title = title_match.group(0).replace('## ', '') if title_match else psg['slide']

        if summary:
            result.append({
                'doc': doc_path,
                'slide': psg['slide'],
                'title': title,
                'summary': summary,
                'entity': psg['entity'],
            })

    # 去重
    seen_summaries = set()
    unique = []
    for r in result:
        if r['summary'] not in seen_summaries:
            seen_summaries.add(r['summary'])
            unique.append(r)
    return unique


def search_fulltext(query: str, vault: str, max_results: int = 5) -> List[Dict]:
    """在 vault 所有 .md 文件中全文搜索。"""
    results = []
    keywords = [t for t in tokenize_query(query) if len(t) >= 2]

    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if not fname.endswith('.md'):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, 'r') as f:
                    content = f.read()
            except Exception:
                continue

            # 搜索摘要行
            for line in content.split('\n'):
                if '📝 摘要' not in line:
                    continue
                if any(kw.lower() in line.lower() for kw in keywords):
                    m = re.search(r'> \*?\*?📝\s*摘要\*?\*?[：:]\s*(.+)', line)
                    if m:
                        rel_path = os.path.relpath(filepath, vault)
                        results.append({
                            'doc': rel_path,
                            'text': m.group(1),
                        })

    return results[:max_results]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "请提供查询问题"}, ensure_ascii=False))
        sys.exit(1)

    query = ' '.join(sys.argv[1:])
    graph = load_graph()

    # Step 1: 分词并匹配实体
    tokens = tokenize_query(query)
    matched = match_entities(graph, tokens, top_k=8)

    # Step 2: 对每个匹配实体找到关联实体
    entity_ids = [e['id'] for e in matched]
    all_related = {}
    for eid in entity_ids[:4]:  # top-4 实体做关联扩展
        related = get_related_entities(graph, eid, top_k=5)
        for name, weight in related:
            if name not in all_related or weight > all_related[name]:
                all_related[name] = weight

    # Step 3: 从原文提取相关摘要
    search_entity_ids = entity_ids[:6]  # top-6 实体用于溯源
    passages = extract_passages_from_docs(graph, search_entity_ids, VAULT_PATH)

    # Step 4: 全文搜索补充
    fulltext = search_fulltext(query, VAULT_PATH, max_results=3)

    # 组装输出
    output = {
        "query": query,
        "matched_entities": [
            {
                "name": e.get('canonical_name', e['id']),
                "type": e['type'],
                "doc_count": len(e.get('provenance', [])),
            }
            for e in matched
        ],
        "related_concepts": sorted(all_related.items(), key=lambda x: x[1], reverse=True)[:10],
        "source_passages": passages[:12],
        "fulltext_matches": fulltext,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
