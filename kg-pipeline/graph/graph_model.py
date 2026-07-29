"""
图模型模块
基于 NetworkX 构建知识图谱，管理节点和边的增删改查。
"""
import networkx as nx
from typing import Dict, List, Any, Optional, Set, Tuple


class KnowledgeGraph:
    """
    知识图谱模型，封装 NetworkX 图操作。
    使用 MultiGraph 支持同一对节点间的多条不同类型边。
    """

    def __init__(self):
        self.graph = nx.MultiGraph()

    # === 节点操作 ===

    def add_node(self, node_id: str, node_type: str, **attrs) -> None:
        """添加或更新节点。"""
        attrs['type'] = node_type
        if node_id in self.graph:
            # 合并 provenance
            existing = self.graph.nodes[node_id]
            if 'provenance' in attrs and 'provenance' in existing:
                attrs['provenance'] = self._merge_provenance(
                    existing['provenance'], attrs['provenance']
                )
            # 合并 labels（用于 tag/entity）
            if 'labels' in attrs and 'labels' in existing:
                existing_labels = set(existing.get('labels', []))
                existing_labels.update(attrs['labels'])
                attrs['labels'] = sorted(existing_labels)
        self.graph.add_node(node_id, **attrs)

    def remove_node(self, node_id: str) -> None:
        """删除节点。"""
        if node_id in self.graph:
            self.graph.remove_node(node_id)

    def get_node(self, node_id: str) -> Optional[Dict]:
        """获取节点属性。"""
        if node_id in self.graph:
            return dict(self.graph.nodes[node_id])
        return None

    def has_node(self, node_id: str) -> bool:
        """检查节点是否存在。"""
        return node_id in self.graph

    def get_nodes_by_type(self, node_type: str) -> List[Dict]:
        """获取指定类型的所有节点。"""
        result = []
        for node_id, attrs in self.graph.nodes(data=True):
            if attrs.get('type') == node_type:
                result.append({'id': node_id, **attrs})
        return result

    # === 边操作 ===

    def add_edge(self, source: str, target: str, edge_type: str,
                 weight: float = 1.0, provenance: Optional[List[Dict]] = None,
                 edge_key: Optional[str] = None) -> None:
        """添加或更新边。MultiGraph 支持多条同节点对边，用 edge_key/edge_type 区分。"""
        if not self.has_node(source) or not self.has_node(target):
            return
        attrs = {
            'type': edge_type,
            'weight': weight,
            'provenance': provenance or []
        }
        key = edge_key or edge_type

        # 检查是否已存在同 key 的边
        if self.graph.has_edge(source, target, key):
            existing = self.graph.edges[source, target, key]
            if provenance:
                attrs['provenance'] = self._merge_provenance(
                    existing.get('provenance', []), provenance
                )
            attrs['weight'] = max(existing.get('weight', 0), weight)

        self.graph.add_edge(source, target, key=key, **attrs)

    def get_edges_by_type(self, edge_type: str) -> List[Dict]:
        """获取指定类型的所有边。"""
        result = []
        for u, v, attrs in self.graph.edges(data=True):
            if attrs.get('type') == edge_type:
                result.append({
                    'source': u,
                    'target': v,
                    **attrs
                })
        return result

    # === 批量操作 ===

    def remove_nodes_by_file(self, file_path: str) -> None:
        """删除与指定文件相关的所有节点（旧数据清理）。"""
        doc_id = f"doc:{file_path}"
        to_remove = [doc_id]
        for node_id, attrs in list(self.graph.nodes(data=True)):
            provenance = attrs.get('provenance', [])
            if isinstance(provenance, list):
                for p in provenance:
                    if p.get('file') == file_path:
                        to_remove.append(node_id)
                        break
        for node_id in set(to_remove):
            self.graph.remove_node(node_id)

    # === 图统计 ===

    def stats(self) -> Dict[str, int]:
        """返回图统计信息。"""
        node_types = {}
        for _, attrs in self.graph.nodes(data=True):
            nt = attrs.get('type', 'unknown')
            node_types[nt] = node_types.get(nt, 0) + 1

        edge_types = {}
        for _, _, attrs in self.graph.edges(data=True):
            et = attrs.get('type', 'unknown')
            edge_types[et] = edge_types.get(et, 0) + 1

        return {
            'total_nodes': self.graph.number_of_nodes(),
            'total_edges': self.graph.number_of_edges(),
            'node_types': node_types,
            'edge_types': edge_types,
        }

    # === 导出 ===

    def to_dict(self) -> Dict:
        """将图导出为字典格式（用于 JSON 序列化）。"""
        nodes = []
        for node_id, attrs in self.graph.nodes(data=True):
            node_data = {'id': node_id}
            node_data.update(attrs)
            nodes.append(node_data)

        edges = []
        for u, v, key, attrs in self.graph.edges(data=True, keys=True):
            edge_data = {'source': u, 'target': v, 'key': key}
            edge_data.update(attrs)
            edges.append(edge_data)

        return {
            'nodes': nodes,
            'edges': edges,
            'stats': self.stats()
        }

    # === 内部工具 ===

    @staticmethod
    def _merge_provenance(existing: List[Dict], new: List[Dict]) -> List[Dict]:
        """合并两个 provenance 列表，按 file+form 去重。"""
        merged = {}
        for p in existing:
            key = (p.get('file', ''), p.get('form', ''))
            merged[key] = dict(p)

        for p in new:
            key = (p.get('file', ''), p.get('form', ''))
            if key in merged:
                # 合并 slides 列表
                existing_slides = set(merged[key].get('slides', []))
                existing_slides.update(p.get('slides', []))
                merged[key]['slides'] = sorted(existing_slides)
                # 累加 occurrences
                merged[key]['occurrences'] = (
                    merged[key].get('occurrences', 0) + p.get('occurrences', 0)
                )
            else:
                merged[key] = dict(p)

        return list(merged.values())


def make_doc_id(relative_path: str) -> str:
    """生成文档节点 ID。"""
    return f"doc:{relative_path}"


def make_tag_id(tag_name: str) -> str:
    """生成标签节点 ID。"""
    return f"tag:{tag_name}"


def make_entity_id(canonical_name: str) -> str:
    """生成实体节点 ID。"""
    return f"entity:{canonical_name}"


def make_heading_id(doc_path: str, heading_title: str) -> str:
    """生成标题节点 ID。"""
    return f"heading:{doc_path}:{heading_title}"
