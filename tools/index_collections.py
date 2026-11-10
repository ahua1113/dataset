"""Build local CSV indexes for paper records and collected data batches."""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


DATA = Path(__file__).resolve().parents[1] / "chemistry_dataset"
OUTPUT = DATA / "collection_index"


def jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def utc(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def index_papers():
    source = DATA / "papers"
    summary = DATA / "quantitative_analysis/quantitative_summary.json"
    cutoff = utc(json.loads(summary.read_text(encoding="utf-8"))["generated_at"])
    days = defaultdict(list)
    newer = []
    fields = ("collected_at", "doi", "title", "journal", "file", "local_file_exists")
    for record in jsonl(source / "manifest.jsonl"):
        stamp = utc(record.get("collected_at"))
        row = {key: record.get(key) for key in fields}
        row["local_file_exists"] = bool(row["file"] and (source / row["file"]).is_file())
        days[stamp.date().isoformat() if stamp else "undated"].append(row)
        if stamp and stamp > cutoff:
            newer.append(row)
    daily = []
    for day, records in sorted(days.items()):
        name = f"papers_by_utc_date/{day}.csv"
        write_csv(OUTPUT / name, fields, records)
        daily.append(dict(utc_date=day, records=len(records),
                          local_files=sum(row["local_file_exists"] for row in records),
                          index_file=name))
    write_csv(OUTPUT / "paper_days.csv", ("utc_date", "records", "local_files", "index_file"), daily)
    write_csv(OUTPUT / "after_quantitative_report.csv", fields, newer)
    print(f"Papers: {sum(row['records'] for row in daily)} records, {len(daily)} date groups, "
          f"{len(newer)} collected after the quantitative report.")


def index_batches():
    rows = []
    roots = [DATA / name for name in ("public_datasets", "sensor_timeseries", "collection_runs")]
    for root in roots:
        for manifest in sorted(root.rglob("batch_manifest.jsonl")):
            for batch in jsonl(manifest):
                sdf = batch.get("sdf")
                coverage = f"CID {batch['requested_cid_start']}–{batch['requested_cid_end']}"
                if batch["retrieved_count"]:
                    rows.append(("PubChem", "properties", coverage, batch["retrieved_count"],
                                 manifest.with_name("records.jsonl"), manifest))
                if sdf:
                    count = len(sdf["cids"]) if "cids" in sdf else batch["retrieved_count"]
                    data = manifest.parent.parent / sdf["path"].replace("\\", "/")
                    rows.append(("PubChem", sdf["record_type"], coverage, count, data, manifest))
        for manifest in sorted(root.rglob("*.manifest.json")):
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            name = entry.get("raw_file") or entry.get("file")
            if name and "observation_count" in entry:
                coverage = f"{entry['station']} {entry['start']}–{entry['end']} UTC"
                product = entry.get("parameter_code") or entry.get("product") or entry.get("collection")
                rows.append((entry["source"], product, coverage, entry["observation_count"],
                             manifest.parent / name, manifest))
    fields = ("source", "data_type", "coverage", "records", "data_file", "manifest", "local_file_exists")
    write_csv(OUTPUT / "collection_batches.csv", fields, (
        dict(zip(fields, (source, kind, coverage, count, data.relative_to(DATA).as_posix(),
                          manifest.relative_to(DATA).as_posix(), data.is_file())))
        for source, kind, coverage, count, data, manifest in rows
    ))
    print(f"PubChem and sensor batch entries: {len(rows)}.")


if __name__ == "__main__":
    index_papers()
    index_batches()
