---
name: kg-qa
description: |
  基于 AI/IT 运维知识图谱的问答。通过检索知识图谱中的实体、关联关系和原文摘要，
  结合大模型来回答用户问题。每个答案都附带来源追溯（文档名+幻灯片编号）。
tags: [knowledge-graph, qa, rag,运维, aiops]
---

# 知识库问答 (KG-QA)

当用户提出 AI/IT 运维领域的问题时，从知识图谱中检索相关知识，结合原文内容给出带来源追溯的回答。

## 工作流程

### Step 1: 检索

运行检索脚本：

```bash
cd /Users/bo.gong/Documents/accenture/AI应用/AI-KG && python kg-pipeline/retrieval.py "<用户问题>"
```

返回的 JSON 包含：
- `matched_entities` — 匹配到的实体及来源文档数
- `related_concepts` — 共现关联最强的概念（按权重排序）
- `source_passages` — 原文幻灯片摘要，含文档名、幻灯片编号、标题、摘要
- `fulltext_matches` — 全文搜索到的补充段落

### Step 2: 组织上下文

从检索结果中提取：

1. **核心实体** — `matched_entities` 前 5 个
2. **关联概念** — `related_concepts` 前 10 个，说明概念间的关联关系
3. **原文证据** — `source_passages` 中的摘要，这是可直接引用的原文内容
4. **补充信息** — `fulltext_matches` 作参考

### Step 3: 回答问题

- **必须引用来源**：每条关键信息标注 Obsidian 可点击链接，格式为：
  ```
  【来源：[文档名 > 幻灯片编号](obsidian://open?vault=AI&file=<url编码路径>#<url编码标题>)】
  ```
- **构建 Obsidian 链接**：从 `source_passages` 中取 `doc` 和 `slide` 字段，按如下规则拼接：
  - vault 固定为 `AI`
  - `file` = `doc` 字段的 URL 编码值（空格→`%20`、`&`→`%26`）
  - fragment（`#` 之后）= slide 标题的 URL 编码值（如 `Slide%205`）
  - 示例：`obsidian://open?vault=AI&file=Enterprise%20Observability%20%26%20AIOps%2Fobservability-aiops%20strategy%20stage%200.md#Slide%205`
- **URL 编码对照**：
  | 字符 | 编码 | 说明 |
  |------|------|------|
  | 空格 | `%20` | |
  | `&` | `%26` | **必须编码**，否则破坏 query string |
  | `/` | `%2F` | 路径分隔符 |
  | `#` | `%23` | 仅 file 参数中需要 |
- **优先原文摘要**：`source_passages` 中的内容是 PPT 原文，直接引用
- **图谱关联做补充**：`related_concepts` 反映统计共现，可提示用户探索相关概念
- **知识库无法回答时**：诚实告知，说明知识库当前覆盖范围有限
- **结构化呈现**：实现步骤、效果指标等使用表格或列表

### Step 4（可选）: 补充原文细节

如果 `source_passages` 中的摘要不够详细，可以直接读取对应 .md 文件中具体幻灯片的完整内容：

```bash
grep -A 50 "^## Slide N" "/Users/bo.gong/Documents/accenture/obsidian/AI/<文档路径>"
```

## 知识库范围

当前知识库包含以下文档（来自 Obsidian vault `obsidian/AI/`）：

- **Enterprise Observability & AIOps** — 企业可观测性与 AIOps 战略蓝图（33页）
- **Reinventing IT Operations** — IT 运维重塑（43页）

主题覆盖：AIOps、Observability、AgenticAI、Dynatrace、ServiceNow、自主化运维、MTTR、事件管理等。

## 注意事项

- 检索脚本路径是绝对路径，确保在任意目录下都能执行
- **使用 `python` 而非 `python3`**：系统自带 `/usr/bin/python3` 缺少 jieba 等依赖，必须用 conda 环境中的 `python`（`/opt/miniconda3/bin/python`）
- 如果检索结果为空，尝试用英文关键词或同义词重新检索
- 图谱中实体间 cooccurs_with 权重仅反映标签共现频率，不代表因果关系
