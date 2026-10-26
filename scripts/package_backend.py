import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DIRECTORIES = ("backend", "ml_audit", "ml_features", "ml_ranking", "ml_evaluation",
               "schemas", "rules", "prompts", "tests", "examples", "docs", "scripts", "config", "bce_lab")
FILES = ("README.md", "START_HERE.md", "Dockerfile", "docker-compose.yml", ".dockerignore", ".gitignore",
         "requirements-backend.txt", "requirements-test.txt", "requirements-ml.txt", "requirements-dev.txt",
         "requirements.lock.txt", "THIRD_PARTY_NOTICES.md")
FORBIDDEN_PARTS = {"__pycache__", ".git", ".venv", ".venv_backend", ".venv_win", "data", "private", "raw", "lab_results"}
FORBIDDEN_NAMES = {"lab.config.json", "tariffs.json", ".env"}


def main():
    output = ROOT / "dist" / "alfa-context-backend.zip"
    output.parent.mkdir(exist_ok=True)
    selected = {ROOT / name for name in FILES if (ROOT / name).is_file()}
    for name in DIRECTORIES:
        directory = ROOT / name
        if directory.is_dir():
            selected.update(path for path in directory.rglob("*") if path.is_file())
    paths = []
    for path in sorted(selected):
        relative = path.relative_to(ROOT)
        # bce_lab/data contains committed synthetic fixtures and is permitted.
        parts = set(relative.parts)
        denied = FORBIDDEN_PARTS - ({"data"} if relative.parts[0] == "bce_lab" else set())
        if parts & denied or any(part.startswith(".api-test-") or part.startswith(".ingestion-") for part in relative.parts):
            continue
        if path.name in FORBIDDEN_NAMES or path.name.startswith(".env") or path.suffix.lower() in {".pyc", ".pyo", ".db", ".sqlite3", ".csv", ".zip"}:
            continue
        if path.name in {"lab.config.json", "tariffs.json"}:
            continue
        paths.append(path)
    manifest = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            archive.write(path, "alfa-context-backend/" + path.relative_to(ROOT).as_posix())
        archive.writestr("alfa-context-backend/ARCHIVE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Archive integrity check failed: {bad}")
    print(json.dumps({"archive": str(output), "files": len(paths) + 1, "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
