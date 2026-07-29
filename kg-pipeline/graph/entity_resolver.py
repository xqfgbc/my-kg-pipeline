"""
实体解析器
将提取到的标签/实体归一化到规范名称。
实现三层对齐：归一化 → 别名查表 → 未匹配标记。
"""
from typing import Dict, List, Optional, Set


class EntityResolver:
    """实体别名归一化解析器。"""

    def __init__(self, alias_map: Optional[Dict[str, List[str]]] = None):
        """
        alias_map: {canonical_name: [alias1, alias2, ...]}
        """
        self.alias_map = alias_map or {}
        # 构建反向索引: alias -> canonical_name
        self._alias_index: Dict[str, str] = {}
        self._build_index()

        # 未匹配的实体变体列表（待人工审核）
        self.unmatched: Set[str] = set()

    def _build_index(self) -> None:
        """构建从别名到规范名的反向索引。"""
        self._alias_index = {}
        for canonical, aliases in self.alias_map.items():
            # 规范名本身也映射到自己
            self._alias_index[canonical.lower().strip()] = canonical
            for alias in aliases:
                key = alias.lower().strip()
                if key in self._alias_index and self._alias_index[key] != canonical:
                    # 冲突：同一个别名映射到多个规范名，保留第一个
                    pass
                else:
                    self._alias_index[key] = canonical

    def normalize(self, name: str) -> str:
        """Step 1: 归一化 — trim + 统一小写。"""
        return name.strip().lower()

    def resolve(self, name: str) -> str:
        """
        解析实体名称到规范名。
        Step 1+2: 归一化 → 别名查表
        如果找不到映射，返回归一化后的名称并标记为未匹配。
        """
        normalized = self.normalize(name)
        if normalized in self._alias_index:
            return self._alias_index[normalized]
        else:
            # 未匹配，记录下来
            self.unmatched.add(name.strip())
            return name.strip()

    def resolve_batch(self, names: List[str]) -> List[str]:
        """批量解析实体名称。"""
        return [self.resolve(name) for name in names]

    def get_unmatched(self) -> List[str]:
        """获取所有未匹配的实体变体。"""
        return sorted(self.unmatched)

    def add_alias(self, canonical: str, alias: str) -> None:
        """动态添加别名映射。"""
        if canonical not in self.alias_map:
            self.alias_map[canonical] = []
        if alias not in self.alias_map[canonical]:
            self.alias_map[canonical].append(alias)
        # 更新索引
        key = alias.lower().strip()
        self._alias_index[key] = canonical

    def get_canonical_names(self) -> List[str]:
        """获取所有规范名称列表。"""
        return sorted(self.alias_map.keys())
