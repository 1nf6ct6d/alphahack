"""Immutable streaming CSV staging. Financial calculations stay in ml_features."""

import csv
from pathlib import Path

from ml_audit.audit import REQUIRED_COLUMNS, NUMERIC_COLUMNS, numeric_value
from ml_audit.csv_io import detect_encoding, file_hash, normalized

from .db import Database
from .domain import canonical_json, sha256_json, utc_now
from .imports import ImportTracker


def import_dataset(database: Database, kind: str, csv_path: str | Path, *, encoding: str = "auto") -> dict:
    if kind not in REQUIRED_COLUMNS:
        raise ValueError("kind must be companies, payments, or links")
    path = Path(csv_path)
    if not path.is_file():
        raise ValueError(f"Dataset file does not exist: {path}")
    source_hash = file_hash(path)
    identifier = "csv-" + sha256_json([kind, source_hash])[:24]
    with database.connection() as connection:
        row = connection.execute("SELECT * FROM dataset_snapshots WHERE id = ?", (identifier,)).fetchone()
    if row is not None:
        return {"snapshot_id": identifier, "rows": row["row_count"], "rows_with_issues": row["error_count"], "reused": True}
    tracker = ImportTracker(database)
    run = tracker.start("dataset_" + kind, source_ref=str(path.resolve()), manifest_hash=source_hash)
    try:
        selected_encoding = detect_encoding(path, encoding)
        with path.open("r", encoding=selected_encoding, newline="") as stream:
            sample = stream.read(65536)
            stream.seek(0)
            if not sample.strip():
                raise ValueError("Dataset is empty")
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ";" if sample.splitlines()[0].count(";") > sample.splitlines()[0].count(",") else ","
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            header = next(reader, [])
            if len(header) != len(set(header)):
                raise ValueError("Dataset has duplicate column names")
            missing = set(REQUIRED_COLUMNS[kind]) - set(header)
            if missing:
                raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")
            count = issues_count = 0
            # One writer transaction provides atomic publication. Rows are
            # streamed in bounded batches; no dataset is loaded into memory.
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO dataset_snapshots(id, kind, source_hash, source_filename,
                    encoding, delimiter, header_json, row_count, error_count, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)""",
                    (identifier, kind, source_hash, path.name, selected_encoding, delimiter, canonical_json(header), utc_now()),
                )
                batch = []
                for count, cells in enumerate(reader, 1):
                    issues = []
                    value = None
                    if len(cells) != len(header):
                        issues.append({"code": "column_count", "expected": len(header), "actual": len(cells)})
                    else:
                        value = {key: normalized(cell) for key, cell in zip(header, cells)}
                        for key in set(header) & NUMERIC_COLUMNS:
                            number, state = numeric_value(value[key])
                            if state not in {"missing", "number"} or (number is not None and number < 0):
                                issues.append({"code": "invalid_numeric", "field": key})
                    if issues:
                        issues_count += 1
                    batch.append((identifier, count, reader.line_num, canonical_json(cells),
                                  canonical_json(value) if value is not None else None, canonical_json(issues)))
                    if len(batch) >= 1000:
                        connection.executemany("INSERT INTO dataset_rows VALUES (?, ?, ?, ?, ?, ?)", batch)
                        batch.clear()
                if batch:
                    connection.executemany("INSERT INTO dataset_rows VALUES (?, ?, ?, ?, ?, ?)", batch)
                if file_hash(path) != source_hash:
                    raise ValueError("Dataset changed during import; retry with a stable file")
                connection.execute("UPDATE dataset_snapshots SET row_count = ?, error_count = ? WHERE id = ?", (count, issues_count, identifier))
        stats = {"snapshot_id": identifier, "rows": count, "rows_with_issues": issues_count, "reused": False}
        tracker.succeed(run.import_id, stats=stats, manifest_hash=source_hash)
        return stats
    except Exception as error:
        tracker.add_error(run.import_id, code="dataset_import_failed", message=str(error))
        tracker.fail(run.import_id, str(error))
        raise


__all__ = ["import_dataset"]
