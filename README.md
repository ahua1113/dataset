# 化学源数据采集 V6.4 — OA-only 全学科持续采集

V6.4 是 V6 系列最终执行版。系统只采集无需登录即可合法访问的 OA 论文；期刊用于身份校验和溯源，13 个化学学科的覆盖情况决定调度顺序。

## 采集与计数规则

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
└── quantitative_analysis/
    ├── quantitative_report.csv
    ├── quantitative_summary.json
    ├── papers_inventory.csv
    └── README.md
```

`quantitative_report.csv` 是唯一量化统计长表，统一包含：总量、期刊、主学科、发表年份与月份、出版社、OA 状态、PDF 来源、OA 版本、许可证、采集器版本、文章类型、文件大小、数据质量，以及“学科—期刊”和“学科—年份”交叉计数。`papers_inventory.csv` 是论文级明细，不属于聚合统计。

当前仓库基线已按旧 manifest 的 SHA-256 恢复并重新去重：517 篇、39 本期刊，
发表时间为 2013—2026 年。仍有 5 个 PDF 无法恢复论文身份；另有 103 篇虽有元数据，
但现有可审计关键词不足，因此合计 108 篇保留为“未分类”，不强行计入学科配额。
这些记录仍计入论文总量、文件状态和数据质量统计。

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

## 安装与一次性运行

```powershell
python -m pip install -r requirements.txt
$env:OPENALEX_API_KEY="OpenAlex API Key"
$env:COLLECTOR_CONTACT_EMAIL="联系邮箱"
python collector.py --categories papers --journal-stage all --download
```

联系邮箱用于礼貌性 User-Agent 和 Unpaywall，不写入输出文件。一次性运行使用配置中的每学科 20 篇最低目标。

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
