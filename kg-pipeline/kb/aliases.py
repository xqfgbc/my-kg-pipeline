"""
别名词典 —— 两类来源，职责不同。

1. `aliases.curated.yaml`（进版本库，人工沉淀）
   `entity_aliases: {规范名: [别名…]}`
   例：`Observability → 可观测性 / 全栈可观测性 / Full-Stack Observability`

2. `aliases.mined.yaml`（自动挖掘，可开关）
   `abbreviations: {缩写: 全称}`
   例：`MCP → Model Context Protocol`

⚠️ 这里**不读** `config.aliases.local.yaml` —— 那是旧管道的运行时产物，
   含 76 条由「英文是中文名子串」规则产生的错误映射
   （`Agent治理 → Agent`、`BuildBuy权衡 → Build`）。那些映射会把概念合并掉。

两条扩展规则（都只影响**召回**，不影响实体身份）：
   a. 精确/归一化匹配 → 加入该组的全部写法
   b. 包含匹配（较短者 ≥2 字，长度差 ≤8）→ 例：`可观测` 命中 `可观测性`
"""
import os
import re

import yaml

from .normalize import normalize_term
from .tokenize import query_terms

_HERE = os.path.dirname(os.path.abspath(__file__))
_KG = os.path.dirname(_HERE)                       # kg-pipeline/
CURATED_PATH = os.path.join(_KG, 'aliases.curated.yaml')
MINED_PATH = os.path.join(_KG, 'aliases.mined.yaml')

MAX_LEN_DIFF = 8
MAX_EXPANSION = 40                                 # 防止一次扩展把查询撑爆

_INDEX = None
_FORMS = None


def _load(use_mined: bool = True):
    global _INDEX, _FORMS
    if _INDEX is not None:
        return
    index, forms = {}, {}

    def add(canonical: str, aliases):
        canonical = canonical.strip()
        if not canonical:
            return
        all_forms = [canonical] + [a for a in aliases if a]
        forms[canonical] = all_forms
        for f in all_forms:
            key = normalize_term(f)
            if key and key not in index:            # 先到先得，结果与文件顺序无关
                index[key] = canonical

    try:
        data = yaml.safe_load(open(CURATED_PATH, encoding='utf-8')) or {}
        for canonical, aliases in (data.get('entity_aliases') or {}).items():
            add(canonical, aliases if isinstance(aliases, list) else [aliases])
    except FileNotFoundError:
        pass

    if use_mined:
        try:
            data = yaml.safe_load(open(MINED_PATH, encoding='utf-8')) or {}
            for abbr, full in (data.get('abbreviations') or {}).items():
                add(full, [abbr])
        except FileNotFoundError:
            pass

    _INDEX, _FORMS = index, forms


def load(use_mined: bool = True):
    _load(use_mined)
    return _INDEX, _FORMS


def canonical_of(name: str):
    """名字 → 规范名（找不到则返回 None）。"""
    _load()
    return _INDEX.get(normalize_term(name))


def expand(terms, use_mined: bool = True):
    """查询扩展：在原有 token 之外补充别名写法（只加召回，不改身份）。"""
    _load(use_mined)
    out = list(dict.fromkeys(terms))
    seen = set(out)

    def push(text):
        for tok in query_terms(text):
            if tok not in seen and len(out) < MAX_EXPANSION:
                seen.add(tok)
                out.append(tok)

    for t in list(terms):
        nt = normalize_term(t)
        if not nt:
            continue
        canonical = _INDEX.get(nt)
        if canonical:
            for form in _FORMS.get(canonical, []):
                push(form)
            continue
        # 包含匹配：`可观测` ↔ `可观测性`
        if len(nt) >= 2:
            for key, canon in _INDEX.items():
                if abs(len(key) - len(nt)) > MAX_LEN_DIFF:
                    continue
                if nt in key or key in nt:
                    for form in _FORMS.get(canon, []):
                        push(form)
    return out
