"""
JSON 导出模块
将 NetworkX 图数据导出为 graph.json 文件。
"""
import json
import os
from typing import Dict, Any
from datetime import datetime


class GraphEncoder(json.JSONEncoder):
    """自定义 JSON 编码器，处理 set、datetime 等特殊类型。"""
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def export_graph(graph_dict: Dict[str, Any], output_path: str) -> str:
    """
    将图字典导出为 JSON 文件。
    graph_dict: KnowledgeGraph.to_dict() 的返回值
    output_path: 输出文件路径
    返回输出文件路径。
    """
    # 添加元数据
    graph_dict['metadata'] = {
        'generated_at': datetime.now().isoformat(),
        'version': '1.0',
        'node_count': len(graph_dict['nodes']),
        'edge_count': len(graph_dict['edges']),
    }

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(graph_dict, f, ensure_ascii=False, indent=2, cls=GraphEncoder)

    file_size = os.path.getsize(output_path)
    return output_path


def export_stats_report(stats: Dict[str, Any], graph_dict: Dict[str, Any]) -> str:
    """生成统计报告文本。"""
    lines = []
    lines.append('=' * 60)
    lines.append('  知识图谱管道 — 运行统计报告')
    lines.append('=' * 60)
    lines.append(f'  生成时间: {datetime.now().isoformat()}')
    lines.append(f'  总节点数: {graph_dict["metadata"]["node_count"]}')
    lines.append(f'  总边数:   {graph_dict["metadata"]["edge_count"]}')
    lines.append('-' * 60)

    node_types = stats.get('node_types', {})
    if node_types:
        lines.append('  节点类型分布:')
        for nt, count in sorted(node_types.items()):
            lines.append(f'    {nt}: {count}')

    edge_types = stats.get('edge_types', {})
    if edge_types:
        lines.append('  边类型分布:')
        for et, count in sorted(edge_types.items()):
            lines.append(f'    {et}: {count}')

    lines.append('=' * 60)
    return '\n'.join(lines)
