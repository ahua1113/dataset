# V5 architecture

```text
                  crawl/crawl_papers.py
                           |
                           v
                    collector.py
                           |
                           v
                +---------------------+
                | Multi-source        |
                | scheduler           |
                +----------+----------+
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
          arXiv         Nature        Science
             |             |             |
             +-------------+-------------+
                           |
                           v
                 Metadata normalization
                           |
                           v
                    Global dedup index
              DOI / arXiv / OpenAlex / title
                           |
                           v
                   Chemistry validator
                           |
                           v
                    OA + PDF validation
                           |
                           v
                        Download
                           |
                           v
                     SHA-256 dedup
                           |
                           v
                  raw + manifest + CSV
```

## Failure isolation

```text
arXiv 429
  └─> pause arXiv only

Nature 429
  └─> pause Nature only

Science 429
  └─> pause Science only
```

不存在“主站失败后才启用备用站”的单一 fallback 路径。
