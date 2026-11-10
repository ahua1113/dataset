# 项目与数据目录

| 入口 | 实现 | 测试 |
| --- | --- | --- |
| `collector.py` | `chemistry_collector/`，论文采集、分类与统计 | `tests/paper_tests/` |
| `collect_public_datasets.py` | `public_datasets/`，PubChem 化合物属性与结构 | `tests/public_dataset_tests/` |
| `collect_sensor_timeseries.py` | `sensor_timeseries/`，USGS、NEON 与 NOAA 观测 | `tests/sensor_timeseries_tests/` |

`tools/` 存放数据维护脚本。运行 `python tools/check_openalex_env.py` 可检查 OpenAlex 环境变量是否设置。运行 `python -m unittest discover -s tests -v` 可执行三组测试。

## 采集产物

| 目录 | 内容 |
| --- | --- |
| `chemistry_dataset/papers/` | 论文清单和本地 PDF。清单中的文件路径以此目录为基准 |
| `chemistry_dataset/quantitative_analysis/` | 论文统计报告和生成时的盘点摘要 |
| `chemistry_dataset/public_datasets/pubchem/` | PubChem 基础批次 |
| `chemistry_dataset/sensor_timeseries/` | 传感器基础分区 |
| `chemistry_dataset/collection_runs/` | 按运行日期保存的定时采集批次及报告 |
| `chemistry_dataset/audit_history/` | 一次性论文清理记录和清理前清单 |

## 数据索引

在项目根目录运行 `python tools/index_collections.py`，从当前文件生成以下索引。

- `chemistry_dataset/collection_index/paper_days.csv` 汇总按 UTC 采集日期划分的论文记录。逐条记录放在 `papers_by_utc_date/`，并标记对应 PDF 在本机是否存在。
- `chemistry_dataset/collection_index/after_quantitative_report.csv` 列出晚于论文统计摘要生成时间的采集记录。
- `chemistry_dataset/collection_index/collection_batches.csv` 汇总 PubChem 和传感器批次。PubChem 的 CID 区间表示批次请求范围，实际结构数见数量列。数据与清单路径相对于 `chemistry_dataset/`。

索引是从源清单生成的本地视图。文件存在标记只检查本机路径，论文原始 PDF 的完整归档和校验以采集服务器为准。
