import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ml_audit.audit import CompanyAudit
from ml_audit.csv_io import detect_encoding, file_hash, normalized

TECHNICAL_COMPANY_IDS = frozenset({"U_428697"})


@dataclass(frozen=True)
class CsvSource:
    path: Path
    encoding: str
    columns: tuple[str, ...]
    sha256: str

    @classmethod
    def open(
        cls, path: str | Path, required: tuple[str, ...],
        encoding: str = "auto",
    ) -> "CsvSource":
        source_path = Path(path)
        selected = detect_encoding(source_path, encoding)
        with source_path.open(encoding=selected, newline="") as stream:
            header = next(csv.reader(stream, delimiter=";", strict=True), [])
        if not header or any(not name.strip() for name in header):
            raise ValueError(f"Нет корректного заголовка: {source_path}")
        if len(set(header)) != len(header):
            raise ValueError(f"Повтор столбцов: {source_path}")
        absent = sorted(set(required) - set(header))
        if absent:
            raise ValueError(
                f"В {source_path} нет столбцов: {', '.join(absent)}"
            )
        return cls(
            source_path, selected, tuple(header), file_hash(source_path)
        )

    def rows(self) -> Iterator[dict[str, str]]:
        with self.path.open(encoding=self.encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=";", strict=True)
            if tuple(next(reader, [])) != self.columns:
                raise ValueError(f"Изменился заголовок: {self.path}")
            for number, values in enumerate(reader, 1):
                if len(values) != len(self.columns):
                    raise ValueError(
                        f"Неверное число полей в записи {number}: {self.path}"
                    )
                yield dict(zip(self.columns, values))
        if file_hash(self.path) != self.sha256:
            raise ValueError(f"Файл изменился во время чтения: {self.path}")

    def manifest(self) -> dict:
        return {
            "path": self.path.as_posix(),
            "encoding": self.encoding,
            "sha256": self.sha256,
            "columns": list(self.columns),
        }


def load_directory(source: CsvSource) -> CompanyAudit:
    directory = CompanyAudit()
    for row in source.rows():
        directory.observe(row)
    return directory


def profile_status(directory: CompanyAudit, company_id: str) -> str:
    if company_id in TECHNICAL_COMPANY_IDS:
        return "technical_ambiguous"
    if company_id not in directory.records:
        return "missing"
    if company_id in directory.conflicts:
        return "conflicting"
    return "unambiguous"


def confirmed_group(directory: CompanyAudit, company_id: str) -> str | None:
    if profile_status(directory, company_id) != "unambiguous":
        return None
    return normalized(directory.records[company_id].get("GROUP_ID"))
