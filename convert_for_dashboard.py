#!/usr/bin/env python3
"""
将 kg-pipeline-data/graph.json 转换为 understand-anything dashboard 兼容格式。
输出到 .understand-anything/knowledge-graph.json
"""
import json
import os
import sys
from datetime import datetime

KG_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(KG_DIR, "kg-pipeline-data", "graph.json")
OUTPUT_DIR = os.path.join(KG_DIR, ".understand-anything")
OUTPUT = os.path.join(OUTPUT_DIR, "knowledge-graph.json")
META = os.path.join(OUTPUT_DIR, "meta.json")

# 节点类型映射: 我们的类型 → dashboard NodeType
NODE_TYPE_MAP = {
    "document": "article",
    "entity":   "entity",
    "tag":      "topic",
    "heading":  "concept",
}

# 边类型映射: 我们的类型 → dashboard EdgeType
EDGE_TYPE_MAP = {
    "contains":       "contains",
    "cooccurs_with":  "related",
    "references":     "cites",
    "similar_to":     "similar_to",
    "related_to":     "related",
}

# 边方向
EDGE_DIRECTION_MAP = {
    "contains":       "forward",
    "cooccurs_with":  "bidirectional",
    "references":     "forward",
    "similar_to":     "bidirectional",
    "related_to":     "bidirectional",
}


def convert():
    with open(INPUT, 'r') as f:
        src = json.load(f)

    # 转换节点
    nodes = []
    node_ids = set()
    for n in src.get("nodes", []):
        src_type = n.get("type", "concept")
        new_type = NODE_TYPE_MAP.get(src_type, "concept")

        # 构建 name
        name = n.get("canonical_name") or n.get("title") or n.get("name") or n["id"]

        # 构建 summary
        if src_type == "entity":
            provenance = n.get("provenance", [])
            doc_count = len(provenance)
            total_occurrences = sum(p.get("occurrences", 0) for p in provenance)
            summary = f"实体，出现在 {doc_count} 个文档中，共 {total_occurrences} 次引用"
        elif src_type == "document":
            summary = n.get("title", name)
        elif src_type == "tag":
            summary = f"标签: {name}"
        elif src_type == "heading":
            summary = f"标题 (L{n.get('level', '?')}): {name}"
        else:
            summary = name

        # 收集 tags
        tags = n.get("tags", []) or []
        if isinstance(tags, str):
            tags = [tags]

        node = {
            "id": n["id"],
            "type": new_type,
            "name": name,
            "summary": summary[:200],
            "tags": tags[:20],
            "complexity": "simple",
        }

        if src_type == "document":
            node["filePath"] = n.get("path", "")
        if src_type == "entity":
            node["knowledgeMeta"] = {
                "content": summary,
            }

        nodes.append(node)
        node_ids.add(n["id"])

    # 转换边
    edges = []
    for e in src.get("edges", []):
        src_type = e.get("type", "related")
        new_type = EDGE_TYPE_MAP.get(src_type, "related")
        direction = EDGE_DIRECTION_MAP.get(src_type, "bidirectional")

        # 验证 source/target 存在
        if e["source"] not in node_ids or e["target"] not in node_ids:
            continue

        # 构建 description
        provenance = e.get("provenance", [])
        if provenance and src_type == "cooccurs_with":
            doc_count = len(set(p.get("file", "") for p in provenance))
            description = f"共现于 {doc_count} 个文档 (weight={e.get('weight', 0):.2f})"
        elif provenance and src_type == "references":
            occurrences = sum(p.get("occurrences", 0) for p in provenance)
            description = f"引用 {occurrences} 次"
        elif provenance and src_type == "similar_to":
            shared = provenance[0].get("shared_tags", []) if provenance else []
            description = f"共享 {len(shared)} 个标签 (Jaccard={e.get('weight', 0):.3f})"
        else:
            description = e.get("type", "")

        edge = {
            "source": e["source"],
            "target": e["target"],
            "type": new_type,
            "direction": direction,
            "weight": min(max(e.get("weight", 1.0), 0.0), 1.0),
            "description": description[:200],
        }
        edges.append(edge)

    # 构建 layers（按节点类型分组）
    layer_groups = {}
    for n in nodes:
        t = n["type"]
        layer_groups.setdefault(t, []).append(n["id"])

    layers = []
    for i, (nt, ids) in enumerate(sorted(layer_groups.items())):
        layers.append({
            "id": f"layer-{nt}",
            "name": {"article": "文档", "entity": "实体", "topic": "标签", "concept": "标题"}.get(nt, nt),
            "description": f"{len(ids)} 个{nt}节点",
            "nodeIds": ids,
        })

    # 组装输出
    output = {
        "version": "1.0.0",
        "kind": "knowledge",
        "project": {
            "name": "AI/IT运维知识库",
            "languages": ["Markdown"],
            "frameworks": ["Obsidian"],
            "description": f"基于 Obsidian vault 的 AI/IT 运维领域知识图谱，含 {len(nodes)} 个节点、{len(edges)} 条边",
            "analyzedAt": datetime.now().isoformat(),
            "gitCommitHash": "",
        },
        "nodes": nodes,
        "edges": edges,
        "layers": layers,
        "tours": [],
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT, 'w') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 写 meta.json
    with open(META, 'w') as f:
        json.dump({
            "lastAnalyzedAt": datetime.now().isoformat(),
            "version": "1.0.0",
            "analyzedFiles": sum(1 for n in nodes if n["type"] == "article"),
        }, f, indent=2)

    print(f"✅ 转换完成")
    print(f"   节点: {len(nodes)} ({', '.join(f'{k}: {len(v)}' for k, v in layer_groups.items())})")
    print(f"   边:   {len(edges)}")
    print(f"   Layer: {len(layers)}")
    print(f"   输出: {OUTPUT}")


if __name__ == "__main__":
    convert()
