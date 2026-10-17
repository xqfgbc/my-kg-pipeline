"""
归一化 —— 全链路共用同一套规则，避免「匹配用的写法」与「存储用的写法」不一致。

实测背景：语料里有 23 组纯拼写变体各占一个节点
（`AI Agent`↔`AIagent`、`Object Type`↔`ObjectType`、`Human-in-the-Loop`↔`HumanInTheLoop`…）。
旧代码只做 strip+lower，消不掉分隔符差异。
"""
import re
import unicodedata

# 分隔符：空白、连字符、下划线、中点、片假名中点
_SEP = re.compile(r'[\s\-_·・]+')


def normalize_term(text: str) -> str:
    """术语归一 —— 用于【实体身份】比较。

    NFKC（全角→半角、兼容字符归一）→ 去分隔符 → 小写。
    这是「两个名字是否指同一个东西」的判据。
    """
    if not isinstance(text, str):
        return ''
    text = unicodedata.normalize('NFKC', text)
    return _SEP.sub('', text).strip().lower()


def normalize_query(text: str) -> str:
    """查询归一 —— 用于【检索】。

    只做 NFKC 与空白压缩，**保留分隔符**：查询里的空格对英文词组有意义，
    不能像 normalize_term 那样抹掉。
    """
    if not isinstance(text, str):
        return ''
    text = unicodedata.normalize('NFKC', text)
    return re.sub(r'\s+', ' ', text).strip()


def fold(text: str) -> str:
    """轻量折叠 —— 用于「包含匹配」这类宽松比较（查询扩展用）。

    与 normalize_term 的区别：保留字符顺序但不要求完全相等，
    配合 `in` 使用。仅小写 + NFKC，不去分隔符（否则 'a i' 会误命中 'ai'）。
    """
    if not isinstance(text, str):
        return ''
    return unicodedata.normalize('NFKC', text).strip().lower()
