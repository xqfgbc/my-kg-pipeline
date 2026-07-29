"""
统一配置加载器。

加载顺序（后者覆盖前者）：
1. config.yaml（默认值，提交到 Git）
2. config.local.yaml（本地覆盖，不提交到 Git）
3. 环境变量（运行时覆盖）

用法:
    from kg_pipeline_config import get_config
    config = get_config()
    vault_path = config['vault_path']
"""
import os
import yaml
from typing import Dict, Any

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 配置文件路径
_CONFIG_YAML = os.path.join(_PROJECT_ROOT, "kg-pipeline", "config.yaml")
_CONFIG_LOCAL_YAML = os.path.join(_PROJECT_ROOT, "config.local.yaml")

# 缓存
_config_cache: Dict[str, Any] | None = None


def _load_yaml(path: str) -> Dict[str, Any]:
    """加载 YAML 文件，文件不存在时返回空字典。"""
    if os.path.exists(path):
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}
    return {}


def get_config(force_reload: bool = False) -> Dict[str, Any]:
    """获取合并后的完整配置。结果会被缓存。"""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    # Layer 1: 默认配置（提交到 Git）
    config = _load_yaml(_CONFIG_YAML)

    # Layer 2: 本地覆盖（不提交到 Git）
    local = _load_yaml(_CONFIG_LOCAL_YAML)
    config.update({k: v for k, v in local.items() if v is not None})

    # Layer 3: 环境变量覆盖（以 KG_ 为前缀）
    #   KG_VAULT_PATH → vault_path
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
