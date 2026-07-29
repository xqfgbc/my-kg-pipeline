"""
统一配置加载器。

加载顺序（后者覆盖前者）：
1. config.yaml（默认值 + 人工别名，提交到 Git）
2. config.aliases.yaml（自动发现别名，提交到 Git）
3. config.local.yaml（本地覆盖，不提交到 Git）
4. 环境变量（运行时覆盖）

用法:
    from kg_pipeline_config import get_config, get_merged_aliases
    config = get_config()
    vault_path = config['vault_path']
    aliases = get_merged_aliases()
"""
import os
import yaml
from typing import Dict, Any

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 配置文件路径
_CONFIG_YAML = os.path.join(_PROJECT_ROOT, "kg-pipeline", "config.yaml")
_CONFIG_ALIASES_YAML = os.path.join(_PROJECT_ROOT, "config.aliases.yaml")
_CONFIG_LOCAL_YAML = os.path.join(_PROJECT_ROOT, "config.local.yaml")

# 缓存
_config_cache: Dict[str, Any] | None = None
_aliases_cache: Dict[str, Any] | None = None


def _load_yaml(path: str) -> Dict[str, Any]:
    """加载 YAML 文件，文件不存在时返回空字典。"""
    if os.path.exists(path):
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}
    return {}


def _deep_merge(base: Dict, overlay: Dict) -> Dict:
    """深度合并两个字典，嵌套 dict 递归合并（列表直接覆盖）。"""
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def get_merged_aliases(force_reload: bool = False) -> Dict[str, Any]:
    """合并 config.yaml + config.aliases.yaml 的 entity_aliases。"""
    global _aliases_cache
    if _aliases_cache is not None and not force_reload:
        return _aliases_cache

    base = _load_yaml(_CONFIG_YAML).get('entity_aliases', {})
    auto = _load_yaml(_CONFIG_ALIASES_YAML).get('entity_aliases', {})
    merged = _deep_merge(base, auto)

    _aliases_cache = merged
    return merged


def get_config(force_reload: bool = False) -> Dict[str, Any]:
    """获取合并后的完整配置。结果会被缓存。"""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    # Layer 1: 默认配置 + 合并别名（提交到 Git）
    config = _load_yaml(_CONFIG_YAML)
    auto_aliases = _load_yaml(_CONFIG_ALIASES_YAML).get('entity_aliases', {})
    if auto_aliases:
        base_aliases = config.get('entity_aliases', {})
        config['entity_aliases'] = _deep_merge(base_aliases, auto_aliases)

    # Layer 2: 本地覆盖（不提交到 Git）
    local = _load_yaml(_CONFIG_LOCAL_YAML)
    config.update({k: v for k, v in local.items() if v is not None})

    # Layer 3: 环境变量覆盖（以 KG_ 为前缀）
    env_mapping = {
        'KG_VAULT_PATH': 'vault_path',
    }
    for env_key, config_key in env_mapping.items():
        val = os.environ.get(env_key)
        if val:
            config[config_key] = val

    _config_cache = config
    return config


def get_vault_path() -> str:
    """快捷方法：获取 vault 路径。"""
    config = get_config()
    path = config.get('vault_path', '')
    if not path:
        raise RuntimeError(
            "vault_path 未配置！请设置以下任一方式：\n"
            "  1. 复制 config.local.yaml.example 为 config.local.yaml 并修改路径\n"
            "  2. 设置环境变量: export KG_VAULT_PATH=/path/to/vault"
        )
    return path
