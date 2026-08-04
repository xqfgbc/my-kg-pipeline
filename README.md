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
| `kg-pipeline/config.yaml` | ✅ 提交 | 管道参数、排除模式、阈值 |
| `config.aliases.local.yaml` | ❌ 忽略 | 中英双语实体别名映射 |
| `config.local.yaml` | ❌ 忽略 | vault_path 等本地路径 |

环境变量覆盖：

| 变量 | 作用 |
|------|------|
| `KG_VAULT_PATH` | 覆盖 vault 路径（最高优先级） |

## 检索流程

```
用户查询
  → jieba 分词 (中文精确切词 + 英文提取)
  → expand_bilingual (中→英 / 英→中 别名扩展)
  → EntityMatcher (TF-IDF char_wb + cosine similarity → top-8 实体)
  → 图谱共现边扩展 (cooccurs_with)
  → 原文定位 (provenance → 三种策略提取正文，去元信息行，≤3000字)
      • Slide N → 匹配 ## Slide N 标题（ppt-extract 文档）
      • 章节标题 → 匹配 ##/### 标题（web-pdf-extract 文档）
      • unknown → 回退 ## 摘要 章节（文档级标签）
  → rerank_passages (实体分×0.4 + 正文命中×0.6 → top-8)
  → 全文补充 (双格式：📝 摘要行 + ## 摘要章节)
  → JSON 输出 (body + summary + entity)
```

### 输出字段

| 字段 | 说明 |
|------|------|
| `matched_entities` | 匹配实体列表（含类型和文档数） |
| `related_concepts` | 图谱共现关联概念 |
| `source_passages[].body` | 完整正文（≤3000字） |
| `source_passages[].summary` | 摘要（≤300字） |
| `source_passages[].title` | 幻灯片标题或章节标题 |
| `source_passages[].doc` / `.slide` | 来源文档和定位（Slide N / 章节名 / Document Summary） |

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

## Claude Code Skill — `/kg-qa`

此项目同时是一个 Claude Code Skill，通过 `SKILL.md` 定义。`~/.claude/skills/kg-qa` 是指向本目录的符号链接。

### 使用方式

在 Claude Code 中直接调用：

```
/kg-qa <你的问题>
```

例如：

```
/kg-qa MTTR的意义
/kg-qa AIOps怎么结合ServiceNow
```

### 工作原理

1. 运行 `python kg-pipeline/retrieval.py "<问题>"` 检索知识图谱
2. 提取匹配实体、共现概念、原文摘要
3. 结合大模型生成带来源追溯的回答（每条引用附带 Obsidian 可点击深链接）

### 技能文件

| 文件 | 说明 |
|------|------|
| `SKILL.md` | Claude Code Skill 定义（工作流、URL 编码规则、注意事项） |

### 符号链接

```bash
# 建立链接（一次性）
ln -s /Users/bo.gong/Documents/accenture/AI应用/AI-KG ~/.claude/skills/kg-qa
```

## License

MIT
