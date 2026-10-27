# 化学源数据采集 V6.3 — OA-only 多来源兜底与全学科配额

## 版本目标

V6.3 将论文采集目标从“每本期刊固定篇数”改为“化学学科覆盖”。期刊负责限定论文质量与保存来源溯源；多本期刊可以共同完成一个学科的配额。

默认配置包含13个化学学科，每个学科至少20篇，并同时满足：

- 至少来自3本期刊；
- 单一期刊占该学科的比例不超过40%；
- 综述论文占该学科的比例不超过30%；
- 每篇论文只有一个 `primary_discipline` 参与计数；
- `secondary_disciplines` 只用于检索和本体扩充，不重复占用配额。

V6.3 只采集无需登录即可合法访问的 OA 资源，不包含出版商登录、校园网认证、验证码处理、付费墙绕过或 Cloudflare 绕过逻辑。

## 期刊来源池

`config/journals_v6_3.json` 是独立的42刊白名单：34本已验证期刊，加上8本高OA候选刊：

1. JACS Au
2. ACS Omega
3. ACS Materials Au
4. RSC Advances
5. ChemistryOpen
6. Beilstein Journal of Organic Chemistry
7. Beilstein Journal of Nanotechnology
8. Journal of Cheminformatics

这8本期刊仍保留在白名单中以便完整溯源，但正式运行还会读取
`config/journal_policy_v6_3.json`：

- 默认启用：JACS Au、ACS Materials Au、两本 Beilstein 期刊和 Journal of Cheminformatics；
- 默认停用：ACS Omega、RSC Advances、ChemistryOpen；
- 被停用期刊不会删除，可用 `--include-low-yield-journals` 显式重新测试。

该策略来自 V6.3 真实 PDF 测试，而不是元数据候选数量。默认停用的三本期刊分别只成功
6/233、2/358 和 5/197 次。JACS Au 与 ACS Materials Au 虽达到10篇，但设置了单刊尝试
预算，避免再次产生数百次低收益请求。

## 项目结构

`collector.py` 是稳定的命令行入口，主要实现位于 `chemistry_collector/`：

```text
chemistry_collector/
├── cli.py           # 命令行参数与流程调度
├── client.py        # HTTPS、robots.txt、限速和域名熔断
├── models.py        # 期刊配置模型与校验
├── policy.py        # 期刊优先级、排除规则与尝试预算
├── discovery.py     # OpenAlex/Unpaywall发现与论文筛选
├── disciplines.py   # 学科分类、配额和报告
├── analysis.py      # 论文目录合并、去重与量化分析
├── feasibility.py   # 逐刊可行性测试
├── papers.py        # 正式按学科采集
├── storage.py       # 下载、SHA-256、manifest和CSV审计
├── reports.py       # 期刊统计报告
└── resources.py     # 视频、讲义、方法和教材
```

运行产生的 PDF、manifest、错误日志和统计报告统一写入 `chemistry_dataset/`，该目录已整体加入 `.gitignore`。源代码仓库不再保存可重新生成的运行结果；需要长期保存的原始数据应进入独立数据仓库。

## 多来源OA兜底

论文身份必须由白名单期刊的 ISSN 与 OpenAlex Source ID 确认；PDF可以来自其他合法公开位置。程序聚合 OpenAlex locations 与 Unpaywall，按以下顺序尝试：

1. 出版社公开PDF；
2. 与该DOI关联的 arXiv / ChemRxiv / bioRxiv / medRxiv / Research Square 公开预印本；
3. PubMed Central / Europe PMC；
4. 机构知识库；
5. 其他可验证OA仓储。

预印本只作为目标期刊论文的公开副本，不会把任意预印本记录当成期刊论文。可用 `--no-allow-preprint-fallback` 统一禁止上述所有预印本平台兜底；其版本固定记录为 `submittedVersion`，避免被上游元数据误标为接受稿。

每条成功记录保存：

- DOI、OpenAlex ID、期刊、ISSN、出版社；
- 主学科、次学科、分类得分和分类证据；
- OA元数据提供方、实际PDF来源类型、兜底顺序；
- OA版本、许可证、许可证核验状态；
- 下载URL、文件大小、SHA-256和采集时间。

许可证缺失不会被伪装成开放许可证，而是标记为 `license_missing_manual_review`。OA可免费下载不等于允许任意再分发或商用，后续发布数据集时必须按论文许可证处理。

## 13个化学学科

学科配置位于 `config/disciplines_v6_3.json`：

- 有机与合成化学
- 无机与配位化学
- 物理化学
- 分析化学
- 理论与计算化学
- 高分子化学
- 材料与纳米化学
- 催化与表面化学
- 电化学与能源化学
- 生物与药物化学
- 环境与地球化学
- 化学工程与工业化学
- 核化学与放射化学

当前分类器使用标题、OpenAlex主题/关键词、摘要和期刊学科组进行加权分类。它适合作为自动初筛和计数依据，但低分样本仍应在正式本体标注前人工复核。

## 安装

```powershell
python -m pip install -r requirements.txt
```

建议设置 OpenAlex API Key，并通过环境变量提供联系邮箱供礼貌性 User-Agent 和
Unpaywall 使用：

```cmd
setx OPENALEX_API_KEY "你的_API_Key"
setx COLLECTOR_CONTACT_EMAIL "你的邮箱"
```

```powershell
$env:OPENALEX_API_KEY="你的_API_Key"
$env:COLLECTOR_CONTACT_EMAIL="你的邮箱"
```

邮箱不会写入输出文件。

## 推荐运行顺序

### 1. 测试8本新增候选刊

先预览元数据：

```powershell
python collector.py --categories papers --feasibility-only --journal-stage candidate --per-journal-target 10 --dry-run
```

再执行真实PDF可下载性测试：

```powershell
python collector.py --categories papers --feasibility-only --journal-stage candidate --per-journal-target 10 --download --out chemistry_dataset/v6_3_feasibility
```

默认仅测试当前仍启用的5本候选刊。如确需复测全部8本，增加
`--include-low-yield-journals`。

### 2. 预览全学科采集结果

```powershell
python collector.py --categories papers --journal-stage all --dry-run --out chemistry_dataset/v6_3_preview
```

也可以直接运行当前正式采集入口（会下载PDF）：

```powershell
python crawl/crawl_papers.py
```

### 3. 正式采集

```powershell
python collector.py --categories papers --journal-stage all --download --out chemistry_dataset/v6_3_papers
```

如果新增候选刊尚未完成测试，先使用34本已验证期刊：

```powershell
python collector.py --categories papers --journal-stage validated --download --out chemistry_dataset/v6_3_papers
```

### 4. 离线统计与文件审计

```powershell
python collector.py --categories papers --journal-stage all --stats-only --out chemistry_dataset/v6_3_papers
python collector.py --categories papers --journal-stage all --audit-only --out chemistry_dataset/v6_3_papers
python -m unittest discover -s tests -v
```

PowerShell 直接检查 JSONL 时请显式指定 UTF-8，避免 Windows PowerShell 5.1
按系统默认编码读取中文：

```powershell
Get-Content -Encoding UTF8 chemistry_dataset/v6_3_papers/manifest.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json }
```

### 5. 合并已有论文并生成量化分析

先只分析当前 `chemistry_dataset` 下能够发现的所有论文目录，不移动或删除文件：

```powershell
python tools/consolidate_papers.py
```

确认统计无误后，将不同采集批次的 PDF 去重复制到
`chemistry_dataset/papers/raw/papers/`，并生成统一 manifest：

```powershell
python tools/consolidate_papers.py --consolidate
```

最后如需删除已经合并的 `v6_3_feasibility`、smoke、preview 等源目录，必须显式确认：

```powershell
python tools/consolidate_papers.py --consolidate --cleanup-sources --yes
```

建议第一次使用复制模式，不要增加 `--move-files`。量化结果统一保存在
`chemistry_dataset/quantitative_analysis/`，包括：

- `quantitative_summary.json`：总量、重复量、期刊数、时间范围与文件大小；
- `by_journal.csv`、`by_discipline.csv`：按期刊和学科计数；
- `by_publication_year.csv`、`by_publication_month.csv`：按发表时间计数；
- 出版社、OA状态、PDF来源、OA版本、许可证、采集器版本、文章类型、分类来源和文件大小区间统计；
- `discipline_journal_matrix.csv`、`discipline_year_matrix.csv`：交叉统计；
- `data_quality.csv`：缺失 DOI、日期、许可证、未分类和本地文件状态；
- `papers_inventory.csv`：去重后的论文级标准化清单；包含 DOI、相对路径和
  SHA-256，仅在本地生成，不提交到代码仓库。

## 主要输出

```text
chemistry_dataset/v6_3_papers/
├── raw/papers/*.pdf
├── manifest.jsonl
├── errors.jsonl
├── papers_v6_3_candidates.csv
├── papers_v6_3_filtered_out.csv
├── discipline_stats_v6_3.json
├── discipline_stats_v6_3.csv
├── discipline_journal_matrix_v6_3.csv
├── source_fallback_stats_v6_3.json
└── source_fallback_stats_v6_3.csv
```

- `discipline_stats_v6_3.*`：每个学科的数量、缺口、期刊数、最大单刊占比和综述占比；
- `discipline_journal_matrix_v6_3.csv`：学科—期刊来源矩阵；
- `source_fallback_stats_v6_3.json`：出版社、arXiv、ChemRxiv、PMC和机构库的实际使用情况；
- `papers_v6_3_filtered_out.csv`：重复、未分类、配额限制、无可用PDF和下载失败原因；
- `manifest.jsonl`：最终论文级完整溯源记录。

## 核心安全边界

- 仅HTTPS；尊重 robots.txt；限制单域请求频率；
- 不保存账号、Cookie、校园网凭据或认证令牌；
- 403、429、登录跳转和非PDF响应会记录后跳过；
- 同一域名连续发生5次访问拒绝后自动熔断，本轮不再向该域名发送下载请求；
- 每本期刊有独立下载尝试预算，低收益期刊默认不参与正式采集；
- 下载过程使用临时 `.part` 文件，核验PDF签名后原子移动；
- DOI、OpenAlex ID、标题、候选URL和SHA-256多层去重；
- 不会自动删除或覆盖旧数据；历史文件缺失或大小不符时不计入配额；
- 无法达到某个学科配额时输出真实缺口，不伪造计数。

详细架构与字段说明见 `docs/V6_3_DESIGN.md`。
