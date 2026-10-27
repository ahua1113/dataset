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

`config/journals_v6_3.json` 继承 V6.2 已通过筛选的34本期刊，并增加8本高OA候选刊：

1. JACS Au
2. ACS Omega
3. ACS Materials Au
4. RSC Advances
5. ChemistryOpen
6. Beilstein Journal of Organic Chemistry
7. Beilstein Journal of Nanotechnology
8. Journal of Cheminformatics

这8本期刊的状态为 `candidate`，每刊可下载性测试阈值为10篇。建议通过测试后再使用 `--journal-stage all` 参与正式采集。V6.2 中因 OA PDF 测试失败剔除的6本期刊不会被重新加入。

## 多来源OA兜底

论文身份必须由白名单期刊的 ISSN 与 OpenAlex Source ID 确认；PDF可以来自其他合法公开位置。程序聚合 OpenAlex locations 与 Unpaywall，按以下顺序尝试：

1. 出版社公开PDF；
2. 与该DOI关联的 arXiv 公开版本；
3. 与该DOI关联的 ChemRxiv 公开版本；
4. PubMed Central / Europe PMC；
5. 机构知识库；
6. 其他可验证OA仓储。

预印本只作为目标期刊论文的公开副本，不会把任意 arXiv/ChemRxiv 记录当成期刊论文。可用 `--no-allow-preprint-fallback` 禁止预印本兜底。

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

建议设置OpenAlex API Key，并提供联系邮箱供礼貌性 User-Agent 和 Unpaywall 使用：

```cmd
setx OPENALEX_API_KEY ""
```

```powershell
$env:OPENALEX_API_KEY=""
```

邮箱不会写入输出文件。

## 推荐运行顺序

### 1. 测试8本新增候选刊

先预览元数据：

```powershell
python collector.py --categories papers --feasibility-only --journal-stage candidate --per-journal-target 10 --dry-run --contact 1316400989@qq.com
```

再执行真实PDF可下载性测试：

```powershell
python collector.py --categories papers --feasibility-only --journal-stage candidate --per-journal-target 10 --download --contact 1316400989@qq.com --out chemistry_dataset/v6_3_feasibility
```

只有 `journal_feasibility_v6_3.json` 中达到10篇的期刊才应晋级正式来源池。

### 2. 预览全学科采集结果

```powershell
python collector.py --categories papers --journal-stage all --dry-run --contact 1316400989@qq.com --out chemistry_dataset/v6_3_preview
```

也可以直接运行安全入口（默认不下载PDF）：

```powershell
python crawl/crawl_papers.py
```

### 3. 正式采集

```powershell
python collector.py --categories papers --journal-stage all --download --contact 1316400989@qq.com --out chemistry_dataset/v6_3_papers
```

如果新增候选刊尚未完成测试，先使用34本已验证期刊：

```powershell
python collector.py --categories papers --journal-stage validated --download --contact 1316400989@qq.com --out chemistry_dataset/v6_3_papers
```

### 4. 离线统计与文件审计

```powershell
python collector.py --categories papers --journal-stage all --stats-only --out chemistry_dataset/v6_3_papers
python collector.py --categories papers --journal-stage all --audit-only --out chemistry_dataset/v6_3_papers
python -m unittest discover -s tests -v
```

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
- 下载过程使用临时 `.part` 文件，核验PDF签名后原子移动；
- DOI、OpenAlex ID、标题、候选URL和SHA-256多层去重；
- 不会自动删除或覆盖旧数据；历史文件缺失或大小不符时不计入配额；
- 无法达到某个学科配额时输出真实缺口，不伪造计数。

详细架构与字段说明见 `docs/V6_3_DESIGN.md`。
