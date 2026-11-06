# 化学源数据采集 V7

V7 支持开放获取论文、PubChem Compound 和公开传感器时序采集。论文采集覆盖 13 个化学学科，使用期刊信息校验身份并记录来源。V7 是仓库发行版本，论文采集继续使用 V6.4 的配置和输出文件名。

## 安装

使用 Python 3.11 或 3.12 安装依赖。

```powershell
python -m pip install -r requirements.txt
```

## PubChem Compound

按 CID 范围采集分子式、分子量、SMILES、InChI、InChIKey 和分子描述符。属性写入 `pubchem/records.jsonl`，结构写入 `pubchem/sdf/` 下的压缩 SDF。批次清单记录结构类型、CID 和 SHA-256。

```powershell
python collect_public_datasets.py --pubchem-start-cid 1 --pubchem-count 100 --dry-run
python collect_public_datasets.py --pubchem-start-cid 1 --pubchem-count 100
```

默认采集 2D 结构。使用 `--no-pubchem-sdf` 仅采集属性，或使用 `--pubchem-record-type 3d` 补采 PubChem 可提供的 3D 结构。对同一输出目录重跑时，按 CID 和结构类型复用已校验的文件。批次失败会记录在 `pubchem/errors.jsonl`，任务状态为 `completed_with_errors`，退出码为 1。默认输出根目录为 `chemistry_dataset/public_datasets/`，可用 `--out` 修改。

## 传感器时序

USGS Water Data 支持按站点、UTC 日期范围和参数代码下载连续观测，输出 JSONL 和分区清单。以下示例使用流量参数 `00060`。

```powershell
python collect_sensor_timeseries.py --source usgs --product continuous --site 01646500 --parameter-code 00060 --start 2024-01-01 --end 2024-01-02
python collect_sensor_timeseries.py --source usgs --product continuous --site 01646500 --parameter-code 00060 --start 2024-01-01 --end 2024-01-02 --audit-only
```

`USGS_API_KEY` 可选。离线审计使用采集时相同的日期、站点、参数和输出目录，核对分区清单、SHA-256、观测时间和重复记录，并汇总缺失值、单位与质量标记。

NEON 支持 `water-quality` 和 `groundwater`，按站点和月份保存原始 CSV 及清单。设置 `NEON_API_TOKEN`，再将示例中的 `ABCD` 替换为所需的四字母站点代码。

```powershell
python collect_sensor_timeseries.py --source neon --product water-quality --site ABCD --start 2024-01 --end 2024-02
```

传感器默认输出到 `chemistry_dataset/sensor_timeseries/`，可用 `--out` 修改。

## 论文采集与计数规则

- 已有论文与新论文统一存放在 `chemistry_dataset/papers/`，启动时全部重新计数；
- 每篇论文只用一个主学科计入配额，次学科保留但不重复计数；
- 初始最低目标为每学科 20 篇、至少 3 本期刊、单刊不超过 40%、综述不超过 30%；
- 连续模式优先补齐数量最少的学科；全部学科达到本轮目标后，下一轮统一增加 20 篇；
- 低收益期刊策略只限制未来下载，既有成功论文仍计入学科、期刊和年份统计；
- DOI、OpenAlex ID、规范化标题、下载 URL 与 PDF SHA-256 多层去重。

系统不包含登录、Cookie、校园网认证、验证码、付费墙或 Cloudflare 绕过逻辑，也不使用 Sci-Hub。

## 数据目录

```text
chemistry_dataset/
├── papers/
│   ├── raw/papers/*.pdf
│   ├── manifest.jsonl
│   ├── errors.jsonl
│   ├── papers_v6_4_candidates.csv
│   ├── papers_v6_4_filtered_out.csv
│   ├── discipline_stats_v6_4.json
│   └── runtime_state_v6_4.json
├── quantitative_analysis/
│   ├── quantitative_report.csv
│   ├── quantitative_summary.json
│   ├── papers_inventory.csv
│   └── README.md
├── public_datasets/pubchem/
└── sensor_timeseries/
```

`quantitative_report.csv` 是唯一量化统计长表，统一包含：总量、期刊、主学科、发表年份与月份、出版社、OA 状态、PDF 来源、OA 版本、许可证、采集器版本、文章类型、文件大小、数据质量，以及“学科—期刊”和“学科—年份”交叉计数。`papers_inventory.csv` 是论文级明细，不属于聚合统计。

论文数量与覆盖情况以最近生成的 [量化统计摘要](chemistry_dataset/quantitative_analysis/quantitative_summary.json) 为准。未分类论文计入总量、文件状态和数据质量统计。

## 首次迁移已有数据

先备份本地 `chemistry_dataset`，再拉取代码。只分析、不改动源文件：

```powershell
python tools/consolidate_papers.py
```

确认后，把旧批次 PDF 去重复制到统一目录并补齐学科字段：

```powershell
python tools/consolidate_papers.py --consolidate
```

如果旧清理流程已经把 PDF 合并成 `orphan_pdf`，但原 manifest 仍在 Git 历史中，
可严格按 SHA-256 恢复题名、DOI、期刊、年份与学科元数据。当前仓库对应命令为：

```powershell
python tools/recover_manifest_metadata.py `
  --git-ref 21fd18e `
  --git-path chemistry_dataset/papers_dataset/manifest.jsonl `
  --git-path chemistry_dataset/v6_3_feasibility/manifest.jsonl `
  --backup
```

恢复工具不会用文件名或相似标题猜测身份；无法精确匹配 SHA-256 的记录继续保留为
孤立 PDF，并在量化报告中列为未分类。

确认统一目录、manifest 和统计无误后，才可删除旧的 feasibility、smoke、preview 等目录：

```powershell
python tools/consolidate_papers.py --consolidate --cleanup-sources --yes
```

默认采用复制而不是移动，避免首次合并失败时丢失源文件。

## 一次性采集论文

```powershell
$env:OPENALEX_API_KEY="OpenAlex API Key"
$env:COLLECTOR_CONTACT_EMAIL="联系邮箱"
python collector.py --categories papers --journal-stage all --download
```

联系邮箱用于礼貌性 User-Agent 和 Unpaywall，不写入输出文件。一次性运行使用配置中的每学科 20 篇最低目标。

### 按论文总量采集

如果当前阶段优先扩大数据量，可以设置累计有效论文总量目标：

```bash
python collector.py --categories papers --journal-stage all --download \
  --max-total-papers 1000 --max-attempts 6000 --per-page 100 \
  --out chemistry_dataset/papers \
  --analysis-dir chemistry_dataset/quantitative_analysis
```

启用 `--max-total-papers` 后，学科、单刊占比和综述占比只参与统计，
不再拒绝候选论文。`unclassified` 论文允许下载并计入总量；DOI、OpenAlex ID、
标题、下载地址和 PDF SHA-256 去重，以及期刊身份、OA来源和文件签名校验仍然生效。
采集器按有效且去重后的历史记录计算累计总量，并在达到目标后停止。

### 每小时新增固定数量并重新分类

服务器可以持续执行“每轮新增100篇”的总量优先采集：

```bash
python collector.py --categories papers --journal-stage all --download \
  --continuous --papers-per-cycle 100 --cycle-interval-seconds 3600 \
  --max-cycles 0 --max-attempts 6000 --per-page 100 \
  --out chemistry_dataset/papers \
  --analysis-dir chemistry_dataset/quantitative_analysis
```

每轮以启动时的有效论文总量加100作为累计目标；如果当轮受网络或候选数量
限制而未补足，下一次重试继续使用同一个目标。达到完整的100篇增量后，程序
对全部有效论文重新执行分类，更新定量分析，再等待3600秒开启下一轮。分类结果
不参与下载准入，`unclassified` 同样计入增量。`--max-cycles 0` 表示持续运行，
直到收到停止信号或磁盘空间低于安全阈值。

## 服务器连续运行

前台验证：

```bash
export OPENALEX_API_KEY='OpenAlex API Key'
export COLLECTOR_CONTACT_EMAIL='联系邮箱'
python crawl/crawl_papers.py
```

默认每 3600 秒启动一轮，最低学科达到当前目标后进入下一 20 篇增量轮次。可直接调用主入口调整参数：

```bash
python collector.py --categories papers --journal-stage all --download --continuous \
  --cycle-interval-seconds 3600 --quota-step 20 --report-every 10 \
  --min-free-gb 5 --out chemistry_dataset/papers \
  --analysis-dir chemistry_dataset/quantitative_analysis
```

`deploy/chemistry-collector.service` 是 systemd 模板。修改其中的账号和安装路径后：

```bash
sudo cp deploy/chemistry-collector.env.example /etc/chemistry-collector.env
sudo cp deploy/chemistry-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chemistry-collector
sudo systemctl status chemistry-collector
journalctl -u chemistry-collector -f
```

运行中可读取 `chemistry_dataset/papers/runtime_state_v6_4.json`。磁盘可用空间低于 5 GiB 时程序暂停；临时失败按指数退避；SIGTERM 会在当前安全边界停止并移除锁文件。每轮使用新的网络客户端，使上一轮临时熔断的站点可以重新测试。

## 离线统计、审计与测试

```powershell
python collector.py --categories papers --journal-stage all --stats-only
python collector.py --categories papers --journal-stage all --audit-only
python -m unittest discover -s tests -v
```

分类器使用标题、OpenAlex 主题与关键词、摘要和期刊组进行加权初筛。低分或“未分类”论文应在正式本体标注前人工复核。完整设计见 `docs/V6_4_DESIGN.md`。
