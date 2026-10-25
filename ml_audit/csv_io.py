import codecs
import hashlib
from pathlib import Path

DATASET_FILES = {
    "companies": "df_Uid_data_hack.csv",
    "payments": "df_Uid_payment_hack.csv",
    "links": "df_Fid_Uid_link_hack.csv",
}
MISSING_TOKENS = frozenset({"", "null", "none", "nan", "n/a", "<na>"})
IDENTIFIER_COLUMNS = frozenset({
    "SYNTHETIC_U", "SYNTHETIC_F", "PAYER", "BENEFICIARY", "GROUP_ID",
    "MAINOKVED_CODE",
})


def missing(value: str | None) -> bool:
    return value is None or value.strip().casefold() in MISSING_TOKENS


def normalized(value: str | None) -> str | None:
    return None if missing(value) else value


def discover_datasets(data_dir: Path) -> dict[str, Path | None]:
    candidates: dict[str, list[Path]] = {}
    if data_dir.is_dir():
        for path in sorted(data_dir.rglob("*")):
            if path.is_file() and path.suffix.casefold() == ".csv":
                candidates.setdefault(path.name.casefold(), []).append(path)
    found = {}
    for name, filename in DATASET_FILES.items():
        matches = candidates.get(filename.casefold(), [])
        if len(matches) > 1:
            locations = ", ".join(str(path) for path in matches)
            raise ValueError(
                f"Найдено несколько файлов {filename}: {locations}"
            )
        found[name] = matches[0] if matches else None
    return found


def validate_encoding(path: Path, encoding: str) -> None:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)


def detect_encoding(path: Path, requested: str = "auto") -> str:
    if requested != "auto":
        validate_encoding(path, requested)
        return requested
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        validate_encoding(path, encoding)
        return encoding
    except UnicodeDecodeError:
        if encoding != "utf-8-sig":
            raise
    validate_encoding(path, "cp1251")
    return "cp1251"


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
