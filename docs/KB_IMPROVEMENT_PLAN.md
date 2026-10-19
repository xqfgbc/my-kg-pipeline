# 本地知识库技术方案与实施方案

> 目标：让本地知识库**更准确**——找得到、排得对、可解释、可维护。
> 约束：不重跑 PPT 提取、不改 vault 与 `.md` 正文、不产生任何 API 或模型调用费用。
> 日期：2026-10-18

---

## 1. Context

现有链路是 `PPT → ppt-extract → Obsidian vault → AI-KG 图谱管道 → kg-qa 检索`。22 篇文档、约 158 万字符、4,958 节点、319,768 边。

实测发现**准确性问题是一条因果链，不是零散缺陷**：

```
页级打标（≤10/页）
  → 聚合成文档级并集（中位 152 个 tag/篇，最多 368）
  → 共现却在【文档级】计算（pipeline.py:325）
  → 每篇 C(152,2) ≈ 11,000 对 → 全图 313,762 条边
  → 98.1% 只由单篇文档支持；Jaccard 在 n=1 时恒等于 1.0
  → 按权重排序 → 91% 实体的 top-8 关联被单文档噪声占据
```

同时，检索层本身是三个薄弱环节叠加：

| 环节 | 现状 | 后果 |
|---|---|---|
| 实体匹配 | TF-IDF over 实体名（不含文档/标题） | 文档节点与 48 条文档 aliases **完全不进召回** |
| 全文检索 | 只匹配「📝 摘要」行，`results[:3]` 无排序 | **87.4% 正文永远搜不到**；三个不同查询返回同样三个文档 |
| 图遍历 | 313k 共现边 | 噪声；且 `similar_to`/`related_to`/`contains` 在检索里**从未被调用** |

**最严重的性能事实：重建一次图要 177.8 秒**，因为 `pipeline.py:458-464` 是「配对数 × 文档数 × 标签数」的二次方循环（≈10 亿次 `resolve()`）。而任何一篇文档变更都会触发全量重建。

**本方案的核心判断**：问题不在"要不要换一个更好的算法"，而在**存储与检索的形态错了**——用一个 126MB 的关系型图存 22 篇文档，用一个无排序的子串扫描做全文检索。把它收敛成一个 SQLite 文件，上述问题大部分会自然消失。

---

## 2. 已确认的决策与约束

| 项 | 决定 |
|---|---|
| 范围 | **只改检索与图谱层**；不重跑提取、不重新撰写 `.md` |
| vault | **不修改**（含不回填 frontmatter `title`） |
| 评测 | **只用自动指标**，不建人工标注问答集 |
| 费用 | **0**；不引入付费 embedding、不调用任何 API |
| 模型 | 仅接受**本地、MIT、零调用费**的模型（bge 系列已确认为 MIT） |

### 必须守住的契约（探查后确认）

| 项 | 约束 |
|---|---|
| state 记录 | `{mtime, hash, extraction}`。顶层加兄弟键安全；`extraction` 内加字段必须升 `EXTRACTION_SCHEMA_VERSION`（`pipeline.py:166`），代价是**全量重解析但不写回 vault**（已核实 `:822-824` 只对 `files_to_process` 回写）。本方案**刻意不动** extraction |
| `retrieval.py` 入参 | 无 argparse，`sys.argv[1:]` 直接 join 成查询串 → **不能加 CLI flag** |
| `retrieval.py` 输出 | `query` / `matched_entities[]{name,type,doc_count}` / `related_concepts[][]` / `source_passages[]{doc,slide,title,summary,body,entity}` / `fulltext_matches[]{doc,text}`。`kg-qa/SKILL.md:65-69` 与 `AI-KG/README.md:98-113` 按此消费 |
| `kb-ingest/verify.py:69-79` | 断言 graph.json 的 document 节点数 == vault `.md` 数 → **必须继续产出 graph.json** |
| `kb-ingest/verify.py:41-52`、`kg-qa/SKILL.md:41-54` | 各自硬编码一套 vault 扫描规则，不读 `exclude_dirs`/`exclude_patterns` → **不要改排除规则** |
| `convert_for_dashboard.py` | 读 graph.json，要求每条边有 `source`/`target` |
| `check_changes.py` | 退出码 0/1 契约被 `kg-update/SKILL.md:45-47` 依赖 |

> **关键推论**：不能直接删掉 `graph.json`。正确做法是**把 SQLite 作为主存储，同时导出一份精简的 `graph.json` 作为兼容视图**（只含 doc/slide/term 结构与高置信关系），让 `verify.py`、dashboard、`kg-qa` 的自检继续工作。

---

## 3. 目标架构

### 3.1 分层

```
                    ┌─────────────────────────────┐
  Obsidian vault ──▶│  摄入层（Python）            │
  (.md, 22 篇)      │  解析 → 切片 → 分词 → 术语   │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │  kb.sqlite（单文件，~5–15MB）│
                    │  ├ slides + slides_fts      │  ← BM25
                    │  ├ chunks (+ vec 可选)       │  ← 向量
                    │  ├ terms / term_slides      │  ← 共现（页级）
                    │  └ docs                     │  ← 元数据/别名
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │  检索层 retrieval.py         │
                    │  归一化 → 扩展 → 召回 → 融合  │
                    └──────────────┬──────────────┘
                                   ▼
                    graph.json（导出视图，给 dashboard / verify）
```

### 3.2 为什么是 SQLite

| 指标 | 现状 graph.json | SQLite |
|---|---|---|
| 体积 | 126MB | 词法 4.7MB（实测内存页）/ 带向量 ~15MB |
| 全量重建 | **177.8s** | **< 0.1s**（实测 832 切片） |
| 一次查询 | **12.5s** | 毫秒级（实测 5 次查询 < 0.01s） |
| 增量更新 | 全量重建（O(全局)、二次方） | `DELETE WHERE doc=?` + 插入（O(变更文档)） |
| 依赖 | networkx + 自定义图模型 | Python 内置 `sqlite3` |

**最重要的不是大小或速度，而是复杂度**：SQLite 全量重建 <0.1s，意味着**根本不需要写增量逻辑**——每次全量重建即可。而现在的图必须做增量，否则每次改动付 3 分钟。

### 3.3 数据模型

```sql
-- 文档
CREATE TABLE docs(
  doc_id       TEXT PRIMARY KEY,   -- 目录名（稳定标识）
  title        TEXT,
  aliases      TEXT,               -- JSON array，来自 frontmatter
  doc_type     TEXT, source TEXT, pages INT,
  content_hash TEXT,
  updated_at   TEXT
);

-- 页级切片：BM25 的索引单位，也是引用单位
CREATE TABLE slides(
  slide_id     INTEGER PRIMARY KEY,
  doc_id       TEXT NOT NULL REFERENCES docs(doc_id),
  seq          INT,
  heading      TEXT,               -- "Slide 12: xxx"
  heading_path TEXT,               -- 上级章节链
  body         TEXT,
  body_tokens  TEXT,               -- jieba 预分词（FTS5 用）
  tags         TEXT,               -- JSON array
  chars        INT
);

-- 页级全文索引（列权重：标题 10 / 标签 6 / 正文 1）
CREATE VIRTUAL TABLE slides_fts USING fts5(
  heading, tags, body,
  content='slides', content_rowid='slide_id',
  tokenize='unicode61'
);

-- 编码单元：一页通常 1 段；仅 43 页超长页拆成 2–3 段
CREATE TABLE slide_parts(
  part_id    INTEGER PRIMARY KEY,
  slide_id   INT NOT NULL REFERENCES slides(slide_id),
  ord        INT,
  text       TEXT,
  char_start INT, char_end INT,
  model      TEXT                -- 编码模型版本；换模型时据此重编码
);

-- 向量（阶段 4，模型=bge-m3）
CREATE VIRTUAL TABLE part_vec USING vec0(
  part_id INTEGER PRIMARY KEY,
  embedding FLOAT[1024]          -- bge-m3；页得分取各段最大值
);

-- 术语（替代旧图里的 entity/tag 节点）
CREATE TABLE terms(
  term_id     INTEGER PRIMARY KEY,
  name        TEXT UNIQUE,
  kind        TEXT,              -- concept / acronym / alias
  doc_count   INT, slide_count INT
);
CREATE TABLE term_slides(
  term_id INT, slide_id INT, weight REAL,
  PRIMARY KEY(term_id, slide_id)
);
CREATE TABLE term_aliases(
  alias TEXT PRIMARY KEY,
  term_id INT,
  source TEXT                    -- curated / acronym / normalized
);
```

**与旧图的对应关系**

| 旧 | 新 |
|---|---|
| `entity:` / `tag:` 节点（1,453 对重名） | 合并为 `terms`，一套 |
| `contains` 边（2,895） | `slides.doc_id` + `slide.doc_id` 外键 |
| `cooccurs_with` 边（313,762） | `term_slides` 交集（页级，可回溯到具体页） |
| `similar_to` / `related_to` | 由 `docs.aliases` + 术语交集现算，不落库 |

### 3.4 切片规则（bge-m3，8,192 窗口）

**一级为主**：实测 794 页里只有 **43 页（5.4%）超过 4,000 字符**，所以绝大多数页可以整页编码，不需要子块。

```
文档
└─ 幻灯片  ## 标题        ← BM25 单位 / 引用单位 / 向量单位（794 页）
   └─ 仅超长页（>4,000 字符，43 页）拆成 ≤4,000 字符的段（共 841 段）
```

**规则**

1. 去 frontmatter、去 `## 标题` 行本身、去 `🏷️ 标签` 行（保留 `📝 摘要` 正文）
2. **排除** `## 🔗 关联文档` 与 `幻灯片目录` 整节 —— 实测这两节会稳定抢答
3. 正文 ≤4,000 字符：整页作为 **1 个编码单元**（占 94.6%）
4. 正文 >4,000 字符：按空行（段落 / 列表项 / 表格行）贪心装箱到 ≤4,000，**不在句子中间切**；表格整表为一组，超长则按行分组并**重复表头**
5. 每个编码单元前缀 `"<文档标题> > <幻灯片标题>\n"`
6. **不加重叠**

**结果：794 页 → 841 个编码单元**（bge-small 的 512 窗口方案需要 4,185 个子块，这是选型带来的实质简化）。

**页的向量得分 = 其各段相似度的最大值**；引用仍按页输出。

> ⚠️ **bge-m3 不需要查询指令前缀**（与 bge v1.5 不同，加了反而掉点）。向量需 L2 归一化。
> ⚠️ 512 字符是**安全余量**，不是模型上限：8,192 token 下中文约 1 字 ≈ 1 token，4,000 字符留出了表格与英文词的膨胀空间。

### 3.5 检索流程

```
query
 ├─ 归一化：NFKC（全角→半角）+ 去连字符/空格 + 小写
 ├─ jieba 分词
 ├─ 查询扩展（三层，全部免费）
 │   ├─ 别名表：精确 + 包含匹配（解决「可观测」→「可观测性」）
 │   ├─ 缩写↔全称（141 对，见 §5.3）
 │   └─ 英文词形（agents→agent）
 ├─ 召回
 │   ├─ BM25 over slides_fts（heading/tags/body 加权）
 │   └─ [阶段 4] 向量 kNN over part_vec → 按页取各段最大值
 ├─ 融合：RRF（Reciprocal Rank Fusion），在【页】这一级
 └─ 输出
     ├─ source_passages：top-k 页
     ├─ matched_entities：命中的 terms + doc_count
     └─ related_concepts：term_slides 共现，按 (doc_count, slide_count) 排序
```

**两阶段检索**（免费且有效）：22 篇文档，先用标题/别名把候选收到 1–3 篇（实测文档级路由 FTS 为 100% @1），再在文档内排序。

---

## 4. 增量与变更行为

| 操作 | 现状 | 新方案（词法） | 新方案（+向量） |
|---|---|---|---|
| 新增一篇 | 解析 + **全量重建（177.8s）** + 写 126MB | 解析 → `DELETE WHERE doc_id=?` → 插入该篇切片（**毫秒级**） | 另加编码该篇 ~40 段（bge-m3 CPU 约 **20–40s**） |
| 修改一篇 | 同上，代价与改动大小无关 | 同上 | 同上 |
| 删除一篇 | 全量重建 | `DELETE WHERE doc_id=?`（毫秒级） | 删向量，无计算 |
| 全量重建 | 177.8s | **< 0.1s** | **841 段，约 5–10 分钟**（CPU） |
| 换模型 | 无此概念 | 无 | **必须全量重编码**（`slide_parts.model` 标记版本） |

> **实现选择**：因为全量重建 <0.1s，**首选永远全量重建**，不写增量逻辑。只有向量阶段才需要按文档增量编码。

---

## 5. 分阶段实施

### 阶段 0 · 验证实验（**暂缓**，不改知识库，约 30 分钟）

> **状态：暂缓。** 模型已定为 **bge-m3**；用户决定先建库（阶段 1），不先跑向量实验。
> 本阶段保留为**阶段 4 之前的可选验证**——在花 5–10 分钟编码全库之前，先确认向量确实值得。

目的：把「向量到底能提升多少」从未知变成已知。

| 步骤 | 内容 |
|---|---|
| 0.1 | `pip install fastembed`（~100MB，不装 PyTorch） |
| 0.2 | 编码 832 页（bge-small-zh-v1.5，超窗截断） |
| 0.3 | 跑**非逐字标签代理测试**：用「正文里没有原样出现的标签」查询 |
| 0.4 | 对比基线 **BM25 @1 = 0.4%**，得到向量版真实数字 |
| 0.5 | 若提升显著，再测 bge-m3（8K 窗口，整页编码） |

**为什么这个代理可信**：实测 **53% 的标签在正文里没有原样出现**，它们是同一页内容的另一种措辞。BM25 在这类查询上 @1 = **0.4%**、@8 = **0.9%**；而在「正文里原样出现」的标签上 @1 = 43.6%。这正是词法检索的天花板。

**产物**：一个临时脚本 + 一张对比表，不写入 vault、不改任何配置。

---

### 阶段 1 · 建立 SQLite 基础库

**改什么**

- 新增 `kg-pipeline/kb/store.py`：schema 定义、建库、批量写入、导出 graph.json 视图
- 新增 `kg-pipeline/kb/chunker.py`：按 `## ` 切页，剥离 frontmatter，排除 `🔗 关联文档` / `目录`
- 新增 `kg-pipeline/kb/tokenize.py`：jieba 预分词（含停用与长度规则）
- 新增 `kg-pipeline/build_kb.py`：从 vault 全量建库
- 复用 `kg_pipeline_config.scan_vault_md_files()` 的文件枚举与排除规则（**不要另写一套**）

**验收**

```
832 个页切片入库；slides_fts 覆盖 100% 正文（当前 12.6%）
建库耗时 < 2s；kb.sqlite < 10MB
随机抽 20 页，回 vault 核对 body 长度一致
```

---

### 阶段 2 · 检索层替换

**改什么**

- `kg-pipeline/retrieval.py`：`load_graph()` → `load_kb()`；`search_fulltext()` → `search_bm25()`
- **保持 CLI 与输出 JSON schema 完全不变**（无 argparse、无新 flag）
- 新增两阶段：文档路由 → 段内排序
- `fulltext_matches[].text` 改为 FTS5 高亮片段（语义变化，见 §6 文档同步）

**验收**

```
eval_retrieval.py：
  任务 A（标题自检索）@1 ≥ 95%（基线 50.0%）、@8 = 100%（基线 90.9%）
  任务 B（逐字句定位）@1 ≥ 80%（基线 31.8%）
单次查询 < 200ms（基线 12.5s）
```

---

### 阶段 3 · 术语与组织层

**改什么**

| 项 | 位置 | 动作 |
|---|---|---|
| 别名发现 | `pipeline.py:535-537` | **删除**「英文是中文名子串」规则（76/108 条错别名的来源），保留归一化精确匹配 |
| 反向索引 | `graph/entity_resolver.py:29,32-34` | 修无条件覆写与裸 `pass`；改为确定性裁决并记录冲突 |
| 归一化 | `entity_resolver.normalize` | NFKC + 去分隔符 → 合并 **23 组 / 46 个**拼写变体节点 |
| 大小写 | `entity_resolver.resolve:53-54` | 未匹配时返回归一化名，消除 `CAPEX`/`capex` 双节点 |
| 缩写挖掘 | 新脚本 | 从语料挖 **141 对**「全称（缩写）」（英文模式 + 首字母校验），写入 `config.aliases.mined.yaml` 待人工审核；解析器**不直接读它** |
| 禁词 | `pipeline.py:338-380` | 把 `exclude_tags` 过滤移进建图路径（现在只在回写路径），清除图里 7 个禁用节点 |
| 版式词 | `config.yaml:exclude_tags` | 补 `样例页` `模板页` `章节页` `导航` `章节结构` `标题页`（**不做**统计自动识别——实测篇内 >50% 规则一次都命中不了） |
| 文档标题 | `pipeline.py:330` | 无 `title` 时回退到目录名或首个 `#` 标题，**只在库内生效，不写 vault** |
| 文档别名 | `retrieval.py:126-129` | 22 个文档节点 + **48 条 aliases** 进入 `docs.aliases` 与 BM25 的标题列 |
| 变更谓词 | `check_changes.py:58` vs `pipeline.py:236-244` | 统一为 **hash 单判据**（现在 mtime 未变时 check_changes 永远报「有变更」而管道永不处理） |

**验收**

```
resolve('Build') == 'Build'；resolve('Agent') == 'Agent'；resolve('LLM') 不再指向 GenAI
图书中不含任何 exclude_tags 词（现在 7 个）
不存在 ≤2 字符术语；不存在同一词的大小写/分隔符两个节点（现在 23 组）
不存在 term 与 slide-tag 重名（现在 1,453 对）
related_concepts 每一条都能回溯到具体页
```

---

### 阶段 4 · 向量召回（条件执行：取决于阶段 0 的结果）

**前置**：阶段 0 显示非逐字查询有实质提升。

| 项 | 内容 |
|---|---|
| 模型 | **bge-m3**（1024 维 / 8,192 token / 841 段 / MIT / INT8 ~558MB） |
| 运行时 | `fastembed`（onnxruntime，~100MB），**不要 sentence-transformers**（拖 PyTorch ~2GB） |
| 切片 | §3.4：整页编码，仅 43 页超长页拆段 |
| 索引 | `sqlite-vec`，841 向量 × 1024 维 ≈ **3.4MB**（fp32）；暴力 kNN ≈ 2ms，**不需要 ANN** |
| 查询前缀 | **不需要**（bge-m3 与 bge v1.5 不同） |
| 融合 | RRF（k=60），在页级合并 BM25 与向量结果 |
| 增量 | 按文档重编码（~6s/篇）；`chunks.model` 标记模型版本，换模型时全量重编码 |

**注意**：查询侧需加 BGE 的检索指令前缀（`为这个句子生成表示以用于检索相关文章：`），文档侧不加；向量需 L2 归一化。

**验收**

```
阶段 0 的同一测试：非逐字标签查询 @1 从 0.4% → X%（X 为实测值）
词法指标不得回退（任务 A @1 ≥ 95%，B @1 ≥ 80%）
索引缺失时 retrieval.py 必须优雅降级到纯词法，不能崩
```

---

### 阶段 5 · 兼容与收尾

| 项 | 内容 |
|---|---|
| `graph.json` 导出 | 从 SQLite 导出精简视图（doc/slide/term + 高置信关系），保证 `verify.py:69-79`、`convert_for_dashboard.py`、`kg-qa` 自检继续工作 |
| `kg-qa/SKILL.md` | `fulltext_matches` 语义更新；Step 0 加索引新鲜度检查 |
| `AI-KG/README.md:96-113` | JSON 输出格式说明 |
| `kg-update/SKILL.md` | 建库步骤；**明确禁止用 `--force` 重建**（会重解析并写回 vault） |
| `kb-ingest/verify.py` | 禁词改读 `config.yaml` 且用子串匹配（现在是精确匹配，漏 `Accenture内部`） |
| `config.aliases.yaml` | 死文件（无代码加载）→ 删除，并修 `kg_pipeline_config.py:4-8` 的 docstring |

---

## 6. 实测基线（验收对照表）

| 指标 | 现状 | 目标 |
|---|---|---|
| 共现边数 | 313,762 | ≈11,180（页级）或不再落库 |
| 全文可匹配正文占比 | 12.6% | **100%** |
| 全量重建耗时 | **177.8s** | < 0.1s（词法）/ ~2min（向量） |
| 单次查询耗时 | **12.5s** | < 200ms |
| 存储 | 126MB | 5–15MB |
| 标签词汇表 | 2,336 个（86% 单文档） | 合并去重后重新测量 |
| 别名覆盖 | 86/2336 = 3.7%；76/108 条错误 | 正确率 100%，覆盖含 141 对缩写 |
| 拼写变体重复节点 | 23 组 / 46 个 | 0 |
| 文档节点 title 为路径 | 12/22 | 0 |
| 任务 A（标题自检索）@1 / @8 | 50.0% / 90.9% | ≥95% / 100% |
| 任务 B（逐字句定位）@1 / @8 | 31.8% / 45.5% | ≥80% / ≥90% |
| 任务 C（非逐字标签）@1 | **0.4%** | 词法不承诺；向量阶段实测 |

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `verify.py` 因文档节点数变化误报 | 保留 graph.json 导出视图；不改排除规则；不减文档节点 |
| 归一化合并实体合错 | 23 组全是纯拼写变体，先输出待合并清单人工过目 |
| 向量阶段索引缺失导致 `kg-qa` 崩 | 强制降级路径：检测不到索引就走纯词法 |
| 两个仓库的改动互相依赖 | 每阶段单独 commit；AI-KG 与 kg-tools 分别提交 |
| 检索行为变化未被发现 | 每阶段跑 `eval_retrieval.py` 三个任务，与基线表对比 |

**回滚**：每阶段前 `cp -R kg-pipeline-data kg-pipeline-data.bak.<ts>`（`.gitignore` 已排除 `kg-pipeline-data.bak.*/`）；`kb.sqlite` 与 `graph.json` 都是可重建的派生产物。

---

## 8. 明确不做的事

| 不做 | 理由（实测） |
|---|---|
| 用共现挖别名 | 页级 Jaccard≥0.6 且共现≥3 页，全语料只出 7 对，多为假阳性 |
| 「英文是中文名子串」挖别名 | 方向反了，就是 76/108 条错别名的来源 |
| 用「篇内 >50% 页面」自动识别版式词 | 实测一次都命中不了（导航只占 0–17% 页面） |
| `--force` 重建 | 会全量重解析**并写回 vault** |
| 在 `retrieval.py` 加 CLI flag | 无 argparse，flag 会被当成查询词 |
| 改 `exclude_dirs` / `exclude_patterns` | 两处自检各自硬编码了一套扫描规则，会误报 |
| 重跑提取 / 修 ppt-extract | 用户已排除。留档：连接线只打印字面量 `LINE/CONNECTOR`（30–81 次/份）、GROUP 子形状丢坐标、pptx 通道无字体、PDF 字体摘要恒为 `?` |
| 商用/API 模型 | 约束；所用模型均为 MIT、本地、零调用费 |

---

## 9. 关键技术事实（备查）

| 事实 | 来源 |
|---|---|
| bge-small-zh-v1.5：512 维 / 512 token / ~24M 参数 / fp32 ~90MB | [HF 模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5) |
| bge-m3：1024 维 / 8,192 token / MIT / 558MB(INT8) | [NVIDIA NIM 文档](https://docs.api.nvidia.com/nim/reference/baai-bge-m3) |
| BGE 模型系列 MIT，可商用：「can be used for commercial purposes free of charge」 | FlagEmbedding |
| bge-m3 INT8 中文 R@5 = 0.776（对比 OpenAI 3-small 0.717） | [Zilliz memsearch 评测](https://zilliztech.github.io/memsearch/home/embedding-evaluation/) |
| SQLite FTS5 + `bm25()` 为 Python 内置，零依赖 | 本机 sqlite 3.53.1 实测可用，含 `trigram` 分词器 |
| 页级共现实测 11,180 对（文档级 313,762） | 本次实测 |

---

## 10. 执行顺序

```
0  验证实验（30min，不改知识库）→ 得到向量的真实增益
1  SQLite 基础库（建 schema + 导入 + FTS5）
2  检索层替换（BM25 + 两阶段 + 输出兼容）
3  术语与组织层（别名/归一化/缩写/禁词/标题）
4  向量召回（条件执行）
5  兼容与收尾（graph.json 导出 + 文档同步）
```

**每阶段结束必须**：跑 `eval_retrieval.py` 三个任务 → 与 §6 基线表对比 → 通过才进下一阶段。

---

## 10.5 执行记录

### 阶段 1 · SQLite 基础库 ✅（2026-10-18）

```
文档 22 · 页 798 · 编码段 852
正文 1,398,711 字符
建库 1.66s · 8.05 MB
FTS 覆盖 798/798 = 100%（旧方案 12.6%）
结构章节（🔗 关联文档 / 幻灯片目录）已排除：0 条
```

新增：`kg-pipeline/kb/{__init__,normalize,tokenize,chunker,store}.py`、`kg-pipeline/build_kb.py`

### 阶段 2 · 检索层替换 ✅（2026-10-18）

同一批查询、同一台机器上**新旧两条路径对跑**：

| 任务 | 新 @1 | 旧 @1 | 新 @8 | 旧 @8 | 新耗时 | 旧耗时 |
|---|---|---|---|---|---|---|
| A · 文档标题自检索 | **100.0%** | 50.0% | **100.0%** | 90.9% | **23ms** | 174ms |
| B · 页面独有句定位 | **81.8%** | 36.4% | **100.0%** | 72.7% | **9ms** | 142ms |
| C · 非逐字标签（换说法） | **90.9%** | 63.6% | **100.0%** | 90.9% | **9ms** | 142ms |

- 输出 JSON schema 与旧版**逐键一致**（`query` / `matched_entities{name,type,doc_count}` / `related_concepts` / `source_passages{doc,slide,title,summary,body,entity}` / `fulltext_matches{doc,text}`）
- CLI 无变化（无 argparse、无新 flag），`kg-qa` 无需改动
- `kb.sqlite` 缺失时自动降级到旧路径（`legacy_query`）

**两阶段检索的实现方式**：文档路由是**召回**而不是重排。
实测 `No-Regret Moves to Get Your Cloud AI-Ready` 的文档先验为 1.0，
但它的页完全不在 BM25 top-40 里——只做加权重排救不回来。
因此路由命中的文档，其页会被**直接拉进候选**。
阈值 0.6 是精确信号：模糊查询（「多云运维」「OAM 阶段」「模型上下文协议」）路由数均为 0。

**一处更正**：此前引用的「非逐字查询 @1 = 0.4%」是**裸 BM25 探针**的结果，
不是旧管道（旧管道靠实体 provenance 能拿到 63.6%）。
上表是同一批查询的严格对照。

### 阶段 3 · 术语与组织层 ✅（2026-10-18）

| 项 | 结果 |
|---|---|
| 术语表 | **2,216 个术语 / 3,964 条 term_slide / 2,228 条表面形式** |
| 归一化合并 | `MultiCloud`↔`Multi-Cloud`、`CAPEX`↔`capex`、`Security by Design`↔`SecurityByDesign`、`Utility Agent`↔`UtilityAgent`、`IT 服务管理`↔`IT服务管理` … |
| 禁词 | 建库时过滤 → **库内 0 个**（旧图里 7 个禁用节点 + 版式词枢纽） |
| 别名（人工） | `aliases.curated.yaml`：11 组 / 34 条，从旧表里**剔除 74 条子串错映射**后得到 |
| 别名（挖掘） | `aliases.mined.yaml`：12 对缩写 ← 全称，含 `MCP`、`OAM`、`COTS`、`ETB`、`IAM`、`OSS`、`SRE` |

评测（同一批查询，新旧对跑）：

| 任务 | 新 @1 | 旧 @1 | 新 @8 | 旧 @8 |
|---|---|---|---|---|
| A · 标题自检索 | **100.0%** | 50.0% | **100.0%** | 90.9% |
| B · 页内逐字定位 | **81.8%** | 36.4% | **100.0%** | 72.7% |
| C · 非逐字（换说法） | **95.5%** | 63.6% | **100.0%** | 90.9% |

**两个关键实现经验（都写进了代码注释）**

1. **文档路由必须用用户原话算，不能用扩展后的词集。** 同义词会撑大重合度的分母，0.6 的阈值再也达不到——实测任务 A 从 100% 掉到 50%。
2. **别名扩展只在短查询上开。** 阈值扫描结果：

   | EXPAND_MAX_TERMS | A | B | C |
   |---|---|---|---|
   | 0（关闭） | 100/100 | 82/100 | 91/100 |
   | **3（采用）** | **100/100** | **82/100** | **95/100** |
   | 4 | 100/100 | 77/100 | 95/100 |
   | 99（总是） | 100/100 | 64/100 | 86/100 |

   逐字长句自带足够词项，再补同义词只会稀释 BM25。

**旧管道（`pipeline.py`）的对应项本次未改**：`config.aliases.local.yaml` 已不被 kb 层读取，
子串别名与禁词漏洞不影响检索结果；图里的错误等阶段 5 把 `graph.json` 降为导出视图后自然消失。
若在阶段 5 之前仍需旧图正确，再单独修 `pipeline.py:535-537` 与 `entity_resolver.py:29`。

### 阶段 4 · 向量召回 ✅（2026-10-18，**默认关闭**）

**部署**

| 项 | 值 |
|---|---|
| 模型 | bge-m3 int8 ONNX（ModelScope `Xenova/bge-m3`），**542 MB** |
| 位置 | `~/.cache/ai-kg/bge-m3/`（tokenizer + config + onnx） |
| 运行时 | onnxruntime（经 fastembed 带入，但**没用 fastembed 的模型表**——它没有 bge-m3） |
| 维度 / 归一化 | 1024 / L2，**不加查询前缀**（bge-m3 与 v1.5 不同） |
| 编码量 | 852 段，594 秒（CPU，1.4 段/s） |
| 索引 | 3.49 MB（852 × 1024 × fp32），总库 12.73 MB；暴力余弦 `<1ms` |
| 覆盖 | 798/798 页 |

**嵌入有效性抽查**：`可观测性 ↔ observability` 0.742、`多云管理 ↔ multi-cloud management` 0.829、无关对 ~0.42 —— 模型本身工作正常。

**实测结论：向量在自动指标上不如纯词法**

| 方案 | A @1 | B @1 | C @1 |
|---|---|---|---|
| 纯 BM25（阶段 3） | 100% | **81.8%** | **95.5%** |
| 向量单独 | — | **36.4%** | **63.6%** |
| RRF 等权融合 | 100% | 40.9% | 72.7% |
| 分数归一化融合 w=0.2 | 100% | 68.2% | 95.5% |

三种融合方式、五个权重（0–1.0）都扫过，**没有任何一组超过纯 BM25**。

**为什么**：这两个任务（逐字句定位、关键词标签）本质是**词法任务**——它们用「页面原文」和「作者写的标签」做查询，天生对字面匹配有利。颗粒度不是原因：把页切成 400 字小块重编码，候选内 top-1 命中率反而从 75% 降到 65%。

**但向量在「改了说法」的查询上明显更准**（无自动指标可量化）：

```
「怎么让运维少靠人」
  BM25 : ETB 战略 / AI 架构 / 技术选型
  向量  : 可观测性与AIOps / 重新构想IT运维 / 云战略      ← 明显更对
「云上花销怎么管」
  BM25 : AI Agent 治理 / ETB 战略 / 企业AI架构          ← 跑偏
  向量  : ETB Ch3 / Agentic AI / ETB 战略               ← 更贴题
```

**决定**：`KB_VECTORS=1` 显式开启，**默认关闭**。
理由——在有指标可验证的场景里它没有正贡献，而不能验证的场景不该默认承担它的代价（每次查询 +0.9s 模型加载、+3.5MB 内存）。

> 等有了人工标注问答集，再用它来决定是否把默认打开。届时同一套代码只要改一个默认值。

### 阶段 5 · 兼容与收尾 ✅（2026-10-18）

| 项 | 结果 |
|---|---|
| `graph.json` | **120 MB → 332 KB**（820 节点 / 798 边），由 kb.sqlite 导出 |
| 旧图备份 | `kg-pipeline-data/graph.json.bak-20261018-020542`（120MB，未删除） |
| `kb-ingest/verify.py` | 文档节点 22 == vault 22，**✅ 通过** |
| `check_changes.py` | **exit 0**（22 篇全部未变化） |
| **vault 完整性** | 全量 md5 前后一致 → **一个字节都没改** |
| `verify.py` 禁词 | 改为读 `config.yaml:exclude_tags` + **子串匹配**（原来精确匹配，漏 `Accenture内部`） |
| 死文件 | 删除 `config.aliases.yaml`；修正 `kg_pipeline_config.py` / `pipeline.py` / `retrieval.py` 里描述它的三处 docstring |
| 文档同步 | `kg-qa/SKILL.md`（Step 0 改查 kb.sqlite + 新鲜度）、`kg-update/SKILL.md`（Step 2 改跑 build_kb）、`README.md`（检索流程与输出字段） |

**收尾时发现并修掉的一个坑**：`build_kb.py` 是「删库重建」，会把 `part_vec` 里的向量一起清掉，
而且**毫无提示**——重编码要 10 分钟。已加入向量搬运：按 `(doc_id, seq, ord, 正文 md5)`
匹配，正文没变的段重建后原样搬回。

**评测脚本同步调整**：`graph.json` 降为导出视图后，`legacy_query` 不再可比，
评测脚本改为「当前系统 vs 记录基线」。

### 最终指标（2026-10-18）

| 任务 | 现在 @1 | 现在 @8 | 改造前 @1 | 改造前 @8 |
|---|---|---|---|---|
| A · 文档标题自检索 | **100.0%** | **100.0%** | 50.0% | 90.9% |
| B · 页面独有句定位 | **81.8%** | **100.0%** | 31.8% | 45.5% |
| C · 非逐字标签（换说法） | **95.5%** | **100.0%** | 63.6% | 90.9% |

单次查询 **8–26ms**（改造前 142–175ms），全量重建 **1.6s**（改造前 177.8s），
`kb.sqlite` **12.7MB**（改造前 `graph.json` 120MB）。

### 语料扩充 · 第一批 4 篇（2026-10-19）

| 文档 | 页 | 通道 |
|---|---|---|
| AI ETB - EA Strategy Stage 0 | 49 | python-pptx |
| Navigation Offering Playbook for AI enabled Workforce setup | 46 | pdfplumber |
| AI Operating Model Workshop | 38 | pdfplumber |
| AO Kickoff | 17 | python-pptx |

**结果**

```
文档 22 → 26 · 页 798 → 952 · 术语 2,198 → 2,461
正文 1,398,711 → 1,595,712 字符
建库 3.43s · 13.45 MB（含搬运的 852 条旧向量）
graph.json 视图 978 节点 / 952 边
kb-ingest/verify.py：26 == 26 ✅
```

**评测（改用固定全集，不再抽样——抽样池随语料变化会导致前后不可比）**

| 任务 | n | @1 | @8 |
|---|---|---|---|
| A · 文档标题自检索 | 26 | **100.0%** | 100.0% |
| B · 页面独有句定位 | 100 | 73.0% | 97.0% |
| C · 非逐字标签 | 100 | 85.0% | 96.0% |

按目标文档分组：任务 C 中新文档 @1 77.8% / 旧文档 86.6%——新文档略弱（标签尚未策展），
**但没有拉低旧文档**。

> ⚠️ 旧的 22 抽样基线（B 81.8% / C 95.5%）与新口径**不可直接比较**，因为抽样池不同。

**顺带修掉的一处不一致**：`kg-update` Step 1 原来跑 `check_changes.py`，
而它比的是旧管道的 `index_state.json`——新流程不维护该文件，会永远报「有变更」。
已改为直接比较 kb.sqlite 与 vault 的时间戳。

### 语料扩充 · 506 页 AI ETB（2026-10-19）

**新增**：`Artificial Intelligence Enterprise Technology Blueprint`（506 页）
**停用**：`AI Architecture Framework & Principles`（66 页）——**移出 vault 到
`obsidian/_retired/`，可恢复，不是删除**。它是新 ETB 中被整本取代的早期版本。

选择「整本入库 + 停用旧文档」而不是拆章节，是因为新 ETB 的 506 页里
Technology Trends / Architecture Framework / Ecosystem Overlay / Appendix
四部分是相互引用的整体（附录里大量决策树按前文能力模型编号），拆开会切断这些引用。

**提取**

```
506 页 / 506 张 PNG（150 DPI）/ 27,668 行结构化提取 / 3.1 MB
OCR 506/506
分 6 片撰写（69 / 85 / 84 / 82 / 90 / 96），按 `## Slide N` 切块合并
页码连续性校验 1..506 无缺无重；图片引用逐条校验存在
产物 506 页 / 1.4 MB
```

> 分片给撰写代理的「图片编号统一三位数」指令是**错的**：
> `batch_extract.sh` 用 `printf %02d`，1–99 页是 `slide_01.png`，100 页起才是
> `slide_100.png`。合并脚本 `merge_ebt.py:norm_refs()` 统一改回真实文件名后
> 再逐条断言文件存在——否则 99/506 的图片引用会指向不存在的文件。

**结果**

```
文档 26 → 26（+1 / -1）· Slide 段 1,308 · 切片 1,391（含文档级概要等章节）
术语 2,461 → 3,410
正文 1,595,712 → 2,300,407 字符（新文档贡献 807,226）
编码段 1,472 → 1,461
建库 2.94s · 17.98 MB（搬运 779 段旧向量）
graph.json 视图 1,417 节点 / 1,391 边
kb-ingest/verify.py：26 == 26 ✅
```

**评测**

| 任务 | n | @1 | @8 | 上一轮 @1 |
|---|---|---|---|---|
| A · 文档标题自检索 | 26 | **100.0%** | 100.0% | 100.0% |
| B · 页面独有句定位 | 100 | 72.0% | 95.0% | 73.0% |
| C · 非逐字标签 | 100 | 84.0% | 95.0% | 85.0% |

Slide 段 868 → 1,308（净增 440 页，+51%），指标基本持平——B、C 各 1 个百分点的
波动来自固定全集抽到的页面集合随语料变化，不是质量回退。

> ⚠️ **合并前必须比对分片与产物的 mtime**。第一次合并发生在 14:01:12，
> 而 part3 在 14:05:41、part6 在 14:01:34 才最终落盘——入库的是旧版：
> part3 那 84 页还是未翻译的英文原文，part6 第 506 页的摘要也写着错误的页码范围。
> 重新合并后这 85 页全部更新（正文因此少 7.3 万字符——中文比英文紧凑，不是内容丢失）。
> **教训**：撰写代理是并行且可能在你以为「它报完了」之后还在收尾，
> 合并不应以「块数对得上」为准，应以 `part*.md` 的 mtime 全部早于产物为准。

**一处观察（未改）**：文档级 `## 📋 总体概要` 切片会连同 frontmatter 派生的
摘要与标签一起进入 BM25 索引，因此它可能排在具体页之前。实测 15 个真实查询中
rank-1 只有 1 次是概要切片、top-3 有 3 次，不构成系统性抢答；
且概要本身就是对文档级问题的合理答案。**先记录不改**——调整它会扰动
已建立的评测基线，需要单独测一轮再定。

### 语料扩充 · 研究报告（2026-10-19）

**新增**：`From early impact to enduring advantage`（41 页，Accenture 研究报告）

这是知识库里**第一篇非 PPT 文档**，因此入库方式与前 26 篇不同：

| 项 | PPT 文档 | 本报告 |
|---|---|---|
| 索引单位 | `## Slide N`（一页一块） | `## N. 章节`（一章一块，共 13 块） |
| 页的用途 | 既是切片单位，也是引用单位 | 只是图片引用单位（41 处） |
| 摘要/标签 | 每页一份 | 每章一份 |
| 目录章节 | `幻灯片目录`（已排除） | `报告目录`（已加入 `EXCLUDE_HEADING`） |

**为什么按章节而不按页切**：报告是连续散文，页边界会切断句子和论证；按页切会让
「跨页合并的段落」重新被拆开。章节块 3–8k 字符，对 BM25 仍是合适粒度。

**双栏问题（这次的主要工程量）**

`extract_pdf.py` 用 `extract_text_lines()` 分行，而 pdfplumber 的 line 会把**同一 y
上左右两栏的文字拼进同一个字符串**。第 3 页因此出现：

> Manish Sharma is Chief Strategy and Services Officer Senthil “Sen” serves as Chief Products Officer at

两位作者的简介交错在一起。第 41 页整页都是这个症状。
新增 `kb-work/extract_report_text.py`：词级取坐标 → 找栏间距 → 按栏重排 → 按行距聚段。
判据改过两次，两个坑都值得记：

| 错误判据 | 现象 |
|---|---|
| 「词是否跨过分割线」 | 真双栏页**每一条视觉行左右两侧都有词**，反而全被当成通栏行 |
| 候选分割线步长 4pt | 第 41 页真实栏间距在 316pt，183+4k 只落在 315/319，跨线词从 8 跳到 9 就被判为单栏 |

最终判据：左右两组词各自的 **y 区间重叠 ≥ 较短者的 50%**，且每侧 ≥20% 的词。
实测 41 页中第 3、33、41 页为双栏，其余单栏。

**结果**

```
文档 26 → 27 · 切片 1,391 → 1,404（报告贡献 13 个章节块）
术语 3,410 → 3,452
正文 2,300,407 → 2,326,241 字符（报告贡献 25,834）
建库 4.00s · 18.21 MB
graph.json 视图 1,431 节点 / 1,404 边
kb-ingest/verify.py：27 == 27 ✅
```

**评测**

| 任务 | n | @1 | @8 |
|---|---|---|---|
| A · 文档标题自检索 | 27 | **100.0%** | 100.0% |
| B · 页面独有句定位 | 100 | 72.0% | 96.0% |
| C · 非逐字标签 | 100 | 83.0% | 95.0% |

**检索抽查**（报告专属问题）

| 查询 | 第 1 位 |
|---|---|
| AI 的财务影响为什么是后置的 | 报告 · 2. Material financial impact… |
| Siloed / Structural / Systemic 三个阶段 | 报告 · 7. The three-phase journey… |
| 多数组织为什么没有运营就绪 | 报告 · 3. Most organizations are not operationally ready |
| 86% 的组织 2026 年增加 AI 投资 | 其他文档（报告排第 3） |

> `86%` 那条排第 3 是章节粒度带来的可预期代价：数字落在 8k 字符的章节块里，
> 权重被稀释。若这类「精确数字定位」成为主要用法，应把报告切成小节块（`###` 提升为 `##`）。

### 语料扩充 · Talent Strategy（2026-10-19）

**新增**：`Talent Strategy in the Age of AI`（6 页，The Human+ Approach）

按用户要求「尽量按页分割，即便不能按页分割，也要保证段落完整」处理：
以页为默认切分单位，只有一处例外——**`What you gain` 跨第 4–5 页**
（第 4 页只有第一条「Efficiency without fragility」，其余四条在第 5 页），
保持为同一节，图片按页分别插入，不把一个论点拆成两块。

```
文档 27 → 28 · 切片 1,404 → 1,415（10 个章节 + 总体概要；报告目录不进索引）
术语 3,452 → 3,476
正文 2,326,241 → 2,332,632 字符（+6,391）
建库 2.97s · 18.27 MB
graph.json 视图 1,443 节点 / 1,415 边
kb-ingest/verify.py：28 == 28 ✅
```

| 任务 | n | @1 | @8 |
|---|---|---|---|
| A · 文档标题自检索 | 28 | **100.0%** | 100.0% |
| B · 页面独有句定位 | 100 | **86.0%** | 99.0% |
| C · 非逐字标签 | 100 | 84.0% | 95.0% |

> B 从 72% 跳到 86% **不是检索逻辑变了**：任务 B 的样本是「固定全集里按规则抽 100 页」，
> 语料一变，抽到的页面集合就变。这篇 6 页文档的页面短、主题集中，正好适合逐字句定位。
> 跨语料比较时只能看大致趋势，不能把单轮差值当成提升。

**检索抽查**（7 个该文档专属问题，第 1 位全部命中对应章节：
What's changed / Workforce Reinventor / The leadership imperative / The economics of Human+）。

### 语料扩充 · Advanced AI Talk Track（docx，2026-10-19）

**新增**：`Advanced AI Talk Track`（`AI Messaging 07.14.26.docx`）
知识库第一篇 Word 文档。用 `python-docx` 提取样式结构（125 个非空段落 / 0 图 / 0 表 /
21,244 字符），再按文档自身的消息层级重排：Word 里用 `Title` 样式标记的大消息头
（AI equals outcomes and growth / Human-led AI / Foundations matter）提升为 `##`，
`Heading 2` 及其下的要点作为 `###` 与列表。共 12 个章节块 + 总体概要。

**这份文档暴露并修掉了两个检索缺陷**（都不在 docx 本身）：

**1. 别名包含匹配被用在了英文上**

`expand()` 对长度差 ≤8 的词做子串包含匹配（本意是 `可观测`→`可观测性`）。
但 `ai` 是 `aiops` / `genai` / `agenticai` 的子串，于是查询「AI 是不是泡沫」被扩成：

```
aiops · operations · ops · it · 运维 · 驱动 · agenticai · agentic · workflow
· dynatrace · davis · smartscape · genai · generative · llm · 生成式 …
```

BM25 第 1 名（Talk Track · Reactive Modules，12.05）在最终结果里**连 top-8 都进不去**。
修复：包含匹配只对含中文的词生效（`_CJK.search`）。该查询回到第 1 位。

**2. 长章节没有按 `###` 细分**

报告和 Talk Track 把具体主题放在三级标题下，而索引只按 `##` 切块。
`Reactive Modules` 一章 988 字符包含 5 个议题，主题词被整章稀释。
修复：`##` 章节超过 `SUBSECTION_MIN_CHARS = 1500` 且含 `###` 时，
按 `###` 拆成独立块，继承父章节的标签，`heading_path` 记录 `文档 > 章节 > 小节`。
PPT 文档没有 `###`，不受影响。

| 效果 | 修复前 | 修复后 |
|---|---|---|
| 报告切片 | 13 | 34 |
| Talk Track 切片 | 13 | 20 |
| 「86% 的组织 2026 年增加 AI 投资」 | 第 3 位 | **第 1 位** |
| 「AI 是不是泡沫」 | top-8 之外 | **第 1 位** |

**结果**

```
文档 28 → 29 · 切片 1,415 → 2,021（细分带来的粒度增加）
术语 3,476 → 3,508
建库 3.08s · 15.72 MB（切片变小，体积反而下降）
graph.json 视图 2,050 节点 / 2,021 边
kb-ingest/verify.py：29 == 29 ✅
```

| 任务 | n | @1 | @8 |
|---|---|---|---|
| A · 文档标题自检索 | 29 | **100.0%** | 100.0% |
| B · 页面独有句定位 | 100 | 84.0% | 99.0% |
| C · 非逐字标签 | 100 | **86.0%** | 98.0% |

> B 从 86% 降到 84%，同样**不是检索变差**：任务 B 的样本是「从切片里抽 100 句」，
> 切片细分后抽样池本身变了。C 反而从 85% 升到 86%（别名修复的直接收益）。

**试过但回退的方案**（记录以免重走）：

| 尝试 | 结果 | 原因 |
|---|---|---|
| 先验按纯 IDF 加权 | B 84%→61%，C 85%→64% | 未出现在任何标题中的词权重为 0，长查询分母塌缩，误触发整篇路由 |
| 先验按 IDF + 1.0 基线 | B 84%，C 82% | 长查询保住了，短查询仍有 3 个点损失，收益不足以抵消 |

先验公式最终**保持原样**——「AI 泡沫」那类问题真正的根因是别名扩展，不是先验。

### 语料扩充 · The Near Future of Work（docx，2026-10-19）

**新增**：`The Near Future of Work`（25 页实践指南）
226 个非空段落 / 44,599 字符 / 6 张装饰性插画 / 1 张 9 条术语对照表。

**样式映射**（Word 自定义样式 → Markdown）

| Word 样式 | 含义 | 转成 |
|---|---|---|
| `Kicker` | `AUGUST 2026` / `PART ONE` | 日期行 / 并入 `##` 分部标题 |
| `Part Title` + `Part Lead` | 分部标题与导语 | `##` + 斜体导语 |
| `Heading 1/2/3` | 章节层级 | `##` / `###` / `####` |
| `Lead Paragraph` | 导语段 | 普通段落 |
| `Source Note` | 来源说明 | 列表项 |
| 全大写短行 | 行动号召框 | 引用块 |

四个 PART 各是一个 `##`，其下二级标题是 `###`——超过 1500 字符的章节会被
上一次改动的 `SUBSECTION_MIN_CHARS` 自动拆成独立切片，所以这份 44.6k 字符的文档
产出 33 个切片，而不是 11 个。

```
文档 29 → 30 · 切片 2,021 → 2,054（指南贡献 33）
术语 3,508 → 3,523
正文 2,322,585 → 2,342,625 字符（+20,040）
建库 2.99s · 15.94 MB
graph.json 视图 2,084 节点 / 2,054 边
kb-ingest/verify.py：30 == 30 ✅
```

| 任务 | n | @1 | @8 |
|---|---|---|---|
| A · 文档标题自检索 | 30 | **100.0%** | 100.0% |
| B · 页面独有句定位 | 100 | 84.0% | 99.0% |
| C · 非逐字标签 | 100 | 85.0% | 98.0% |

检索抽查（7 个该文档专属问题）：工作者角色、六个活动、workflow collapse、
30 天测试、八个决策、outcome team 均在第 1 位；「governed autonomy」被另一篇
同主题文档排到前面（合理，那篇整篇讲治理型智能体）。

> ⚠️ **分片时要校验「源文全覆盖」，不能只看边界索引**。
> 这次按 PART 边界切分时，`Conclusion` / `Plain-English terms` / `Source notes`
> 三段落在最后一个边界之外，**不属于任何分片**。负责 PART FOUR 的代理发现
> E.txt 只到 `THE FIRST ACTION`，主动从完整 dump 取回并补全，才没有丢内容。
> 正确的做法：切分后统计所有分片的源行数之和是否等于源文件行数。

## 11. 待定（需要你拍板）

1. ~~向量做不做、用哪个模型~~ —— **已定：bge-m3**（1024 维 / 8K 窗口 / MIT / 本地）。是否在阶段 4 之前先跑阶段 0 的验证实验，待定
2. **是否接受把 `graph.json` 降为导出视图** —— 这是本方案唯一的破坏性变更；保留导出可让现有消费方无感
3. **同义词表由谁维护** —— 缩写 141 对可由脚本挖出，但 `自研↔自建` 这类需要人工确认几十条
