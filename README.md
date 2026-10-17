# My KG Pipeline

AI/IT 运维领域知识图谱构建与检索管道。

## 检索效果

| 特性 | 说明 |
|------|------|
| **jieba 分词** | 中文精确切词，替代 n-gram 暴力切分 |
| **TF-IDF 匹配** | char_wb 字符级 n-gram + cosine similarity，消除短词偏差 |
| **双语扩展** | 别名映射打通中英文跨语言检索 |
| **完整正文** | 返回幻灯片/章节完整内容作为大模型语料（非仅摘要） |
| **段落重排序** | 实体匹配分 × 0.4 + 正文关键词命中 × 0.6 双重排序 |
| **别名自动发现** | 管道运行时自动识别中英双语别名候选 |
| **多文档格式** | 同时支持 ppt-extract（Slide 结构）和 web-pdf-extract（章节结构） |

> 基准测试：实体命中 +33%，原文段落覆盖 +52%，跨语言检索提升最显著。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置本地环境
cp config.local.yaml.example config.local.yaml
# 编辑 config.local.yaml，将 vault_path 改为你的 Obsidian vault 路径

# 3. 配置实体别名（首次使用）
cp config.aliases.local.yaml.example config.aliases.local.yaml
# 编辑 config.aliases.local.yaml 添加中英双语别名映射

# 4. 检查变更
python check_changes.py

# 5. 运行知识图谱管道（含别名自动发现）
python kg-pipeline/pipeline.py

# 6. 检索问答
python kg-pipeline/retrieval.py "你的问题"

# 7. 同步 Dashboard
python convert_for_dashboard.py
```

## 项目结构

```
.
├── kg-pipeline/                 # 知识图谱管道核心
│   ├── pipeline.py              # 主管道（提取→图谱构建→别名发现→回写→导出）
│   ├── retrieval.py             # 检索器（jieba+TF-IDF+双语扩展+重排序）
│   ├── config.yaml              # 默认配置（提交到 Git）
│   ├── extractors/              # 内容提取器
│   ├── graph/                   # 图谱构建模块
│   └── output/                  # 输出模块
├── check_changes.py             # 变更检测
├── convert_for_dashboard.py     # Dashboard 同步
├── kg_pipeline_config.py        # 统一配置加载（三层优先级）
├── config.local.yaml.example    # 本地配置模板（不提交）
├── config.aliases.local.yaml    # 实体别名（不提交，本地维护）
├── requirements.txt             # Python 依赖
└── benchmark/                   # 基准测试数据（不提交）
```

## 配置

| 文件 | Git | 内容 |
|------|-----|------|
| `kg-pipeline/config.yaml` | ✅ 提交 | 管道参数、排除模式、阈值、`exclude_tags` |
| `kg-pipeline/aliases.curated.yaml` | ✅ 提交 | **人工别名**（kb 层读取） |
| `kg-pipeline/aliases.mined.yaml` | ✅ 提交 | **自动挖掘的缩写对**（待审核，kb 层读取） |
| `config.aliases.local.yaml` | ❌ 忽略 | 旧管道的运行时产物，**kb 层不读** |
| `config.local.yaml` | ❌ 忽略 | vault_path 等本地路径 |

环境变量覆盖：

| 变量 | 作用 |
|------|------|
| `KG_VAULT_PATH` | 覆盖 vault 路径（最高优先级） |

## 检索流程

> **当前实现基于 `kb.sqlite`（阶段 1–3）**。旧的 `graph.json` 路径保留为降级方案
> （`kb.sqlite` 缺失时自动使用）。完整方案见 `docs/KB_IMPROVEMENT_PLAN.md`。

```
用户查询
  → 归一化 (NFKC + 空白压缩)
  → jieba 分词
  → 别名扩展 (缩写↔全称 / 拼写变体；仅短查询启用)
  → 阶段 1 · 文档路由：查询与标题/别名的重合度 ≥0.6 时，该文档的页直接进候选
  → 阶段 2 · BM25 over slides_fts（标题×10 / 标签×6 / 正文×1）
  → [可选] 向量召回 (KB_VECTORS=1，本地 bge-m3，分数归一化后加权融合)
  → 按页取 top-8，组装 source_passages
  → JSON 输出
```

**关键设计**：路由是**召回**不是重排——实测某些文档的标题重合度为 1.0，
但它的页根本不在 BM25 候选池里，只做重排救不回来。

### 输出字段

| 字段 | 说明 |
|------|------|
| `matched_entities[]` | `{name, type, doc_count}` —— 命中术语及覆盖文档数 |
| `related_concepts` | `[[name, weight], ...]` —— **同页共现**，证据可回溯到具体页 |
| `source_passages[]` | `{doc, slide, title, summary, body, entity}` |
| `source_passages[].doc` | vault 相对路径（`<目录>/<文件>.md`），`kg-qa` 用它拼 Obsidian 链接 |
| `source_passages[].slide` / `.title` | 页标题（如 `Slide 12: xxx`） |
| `fulltext_matches[]` | `{doc, text}` —— 排名之外的补充页片段 |

> 输出结构刻意与旧版保持一致，`kg-qa` 无需改动。

### 支持的文档格式

管道通过 inline_tag_extractor 的三种标签匹配模式和 retrieval 的三种原文定位策略，同时兼容两种上游提取格式：

| 来源 | 标签格式（管道提取） | 内容定位（检索） |
|------|---------------------|------------------|
| ppt-extract | `> 🏷️ 标签:` / `> **🏷️ 标签**:` | `## Slide N` → 幻灯片正文 |
| ppt-extract | `> **Tags**:` (English variant) | 同上 |
| web-pdf-extract | `> **Tags**:` (document-level) | `## 章节标题` / `## 摘要` 回退 |

> 对应的 Claude Code Skill: `~/.claude/skills/web-pdf-extract/` 和 `~/.claude/skills/ppt-extract/`

## 实体别名

别名映射存储在本地文件 `config.aliases.local.yaml`（不提交到 Git）。

### 手动维护

```yaml
entity_aliases:
  "Observability":
    - "可观测性"
    - "Full-Stack Observability"
  "AgenticAI":
    - "Agentic AI"
    - "Agentic"
```

### 自动发现

管道运行时自动识别中英双语别名候选（子串匹配 + 归一化匹配），追加到同一文件。所有别名在检索时自动用于查询扩展。

## Claude Code Skill

本项目是 [KG Tools](https://github.com/xqfgbc/kg-tools) 工具链的后端实现，提供知识图谱管道和检索能力。对应的 Claude Code Skill 定义（`SKILL.md`）在 KG Tools 仓库中统一管理：

| Skill | KG Tools 路径 | 本项目角色 |
|-------|-------------|-----------|
| `/kg-qa` | `kg-qa/SKILL.md` | 检索问答后端（`kg-pipeline/retrieval.py`） |
| `/kg-update` | `kg-update/SKILL.md` | 图谱更新后端（`kg-pipeline/pipeline.py`、`check_changes.py`） |

### 使用方式

在 Claude Code 中直接调用：

```
/kg-qa <你的问题>
/kg-update                 # 检查变更并增量更新图谱
```

### 符号链接

```bash
# 克隆 KG Tools（一次性）
git clone https://github.com/xqfgbc/kg-tools.git ~/Documents/accenture/AI应用/kg-tools

# 建立 skill 符号链接
ln -s ~/Documents/accenture/AI应用/kg-tools/kg-qa ~/.claude/skills/kg-qa
ln -s ~/Documents/accenture/AI应用/kg-tools/kg-update ~/.claude/skills/kg-update
```

## License

MIT
