# V6.3 OA-only 全学科采集设计

日期：2026-10-27

## 设计原则

V6.3 将“论文质量来源”和“数据集覆盖目标”分开：期刊白名单限定论文身份和来源质量，化学学科配额决定何时停止采集。一个学科可以由多个期刊共同完成，不再要求每本期刊采集相同数量。

## 处理流程

1. 通过期刊 ISSN 解析并校验 OpenAlex Source ID；
2. 仅查询主发表来源匹配该 Source ID 的 article/review；
3. 排除撤稿、旁文本、勘误、社论及无DOI记录；
4. 使用标题、主题、关键词、摘要和期刊组生成唯一主学科及次学科；
5. 检查学科缺口、单刊上限和综述上限；
6. 聚合 OpenAlex 与 Unpaywall 的OA位置并建立兜底链；
7. 按期刊优先级和尝试预算依次下载，核验HTTPS、robots.txt、响应类型、PDF签名、大小和SHA-256；
8. 成功后写入 manifest，并原子更新学科报告和来源矩阵。

连续的401/403/429或robots拒绝会累计到域名熔断器。达到阈值后，本轮直接跳过该域名，
但仍可继续尝试同一论文的机构库、预印本或其他合法OA位置。

## 配额口径

- 13个学科，每学科目标20篇；
- 每学科至少3个期刊；
- 单一期刊最多占40%，目标20篇时最多计8篇；
- 综述最多占30%，目标20篇时最多计6篇；
- 仅主学科计数，次学科不计数；
- 唯一论文按 DOI 优先、OpenAlex ID 次之、SHA-256再次核验。

## 期刊分层

- `validated`：V6.2保留的34刊；
- `candidate`：V6.3新增的8本高OA期刊，每刊可用性测试阈值10篇；
- `all`：两组共同用于采集，仅建议在候选刊通过测试后启用。

期刊白名单与运行策略分离。`journals_v6_3.json` 保留全部42刊以维护溯源，
`journal_policy_v6_3.json` 决定默认启用状态、执行优先级和单刊下载尝试预算。
根据首轮测试，ACS Omega、RSC Advances 和 ChemistryOpen 默认停用；
`--include-low-yield-journals` 只用于有意复测，不作为正式采集默认值。

## 模块边界

- `models.py`：配置模型和输入校验；
- `client.py`：网络边界、限速、robots与熔断；
- `discovery.py`：论文身份和OA位置发现；
- `policy.py`：期刊运行策略；
- `storage.py`：本地文件、manifest和审计记录；
- `disciplines.py`：学科分类、配额与报告；
- `feasibility.py`：逐刊测试；
- `papers.py`：按学科正式采集；
- `resources.py`：视频、讲义、方法和教材；
- `cli.py`：参数解析和流程调度。

根目录 `collector.py` 和 `v6_3_core.py` 仅承担旧命令/导入兼容，不再承载实现。

## OA来源与版本

PDF来源与元数据提供方分开记录。例如，OpenAlex可能发现一份arXiv副本，Unpaywall可能发现一份机构库接受稿：

- `oa_metadata_provider`：`openalex` 或 `unpaywall`；
- `oa_pdf_source`：`publisher`、`arxiv`、`chemrxiv`、`pmc_europepmc`、`institutional_repository` 或 `other_oa_repository`；
- `oa_version`：`publishedVersion`、`acceptedVersion`、`submittedVersion` 或 `unknown`；
- `source_fallback_rank`：本篇论文实际尝试链中的顺序；
- `oa_fallback_chain`：候选URL、来源、版本、许可证和主机的完整列表。

预印本兜底必须满足两个条件：论文已由期刊主来源和DOI确认；预印本位置由 OpenAlex 或 Unpaywall 与该论文关联。预印本不会取代期刊身份字段。

## 分类边界

当前为透明的加权关键词分类器：标题命中权重5，OpenAlex主题/关键词权重3，摘要命中权重1，期刊学科组权重2。最高分为主学科；达到相对阈值的其他学科作为次学科。零分记录进入 `unclassified`，不占配额。

关键词法便于审计和快速迭代，但对交叉学科、缩写和隐含语义仍可能误判。正式用于本体标注前，应根据 `discipline_score` 与 `discipline_evidence` 抽样复核，并可在后续版本替换为受控词表或监督分类模型。
