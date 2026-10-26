"""Bounded PDF text/OCR import using Poppler and Tesseract executables."""

import csv
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from .api_models import BankReportIn


class OCRError(ValueError):
    pass


def _binary(name: str) -> str:
    configured = os.environ.get(f"ALFA_{name.upper()}_BIN", name)
    found = shutil.which(configured)
    if not found:
        raise OCRError(f"{name} is required for PDF import; install it or set ALFA_{name.upper()}_BIN")
    return found


def _run(arguments: list[str], *, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OCRError("PDF/OCR process failed or timed out") from error
    if result.returncode != 0:
        raise OCRError("PDF/OCR tool could not process the document")
    if len(result.stdout) > 2 * 1024 * 1024:
        raise OCRError("PDF/OCR output exceeds the page limit")
    return result.stdout.decode("utf-8", errors="replace")


def extract_bank_pdf(
    path: str | Path, *, source_id: str, title: str,
    report_date: str | None = None, verified: bool = False,
    max_pages: int = 50, language: str = "rus+eng",
) -> dict:
    """Extract native text per page and OCR scanned pages.

    Verification is a separate operator decision, never inferred from OCR
    confidence. Default imports remain unavailable as bank evidence until review.
    """
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
        raise OCRError("PDF must exist and be at most 30 MiB")
    if not 1 <= max_pages <= 100:
        raise OCRError("max_pages must be between 1 and 100")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise OCRError("Input is not a PDF")
        stream.seek(0)
        content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    info = _run([_binary("pdfinfo"), str(path)], timeout=15)
    match = re.search(r"^Pages:\s+(\d+)", info, flags=re.MULTILINE)
    if match is None or not 1 <= int(match[1]) <= max_pages:
        raise OCRError("PDF page count is unavailable or exceeds max_pages")
    fragments = []
    pdftotext = _binary("pdftotext")
    with TemporaryDirectory(prefix="alfa-ocr-") as temporary:
        for page in range(1, int(match[1]) + 1):
            text = _run([pdftotext, "-f", str(page), "-l", str(page), "-enc", "UTF-8", str(path), "-"], timeout=20).strip()
            quality = 1.0
            if len(text) < 80:
                prefix = Path(temporary) / f"page-{page}"
                _run([_binary("pdftoppm"), "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "2400", "-png", str(path), str(prefix)])
                tsv = _run([_binary("tesseract"), str(prefix) + ".png", "stdout", "-l", language, "tsv"])
                words, confidence = [], []
                for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
                    word = (row.get("text") or "").strip()
                    if not word:
                        continue
                    try:
                        score = float(row.get("conf", "-1"))
                    except ValueError:
                        continue
                    if score >= 0:
                        words.append(word)
                        confidence.append(min(score, 100) / 100)
                text = " ".join(words)
                quality = sum(confidence) / len(confidence) if confidence else 0.0
            if text:
                fragments.append({"page_number": page, "text": text[:100000],
                                  "ocr_quality": round(quality, 4), "verified": verified})
    if not fragments:
        raise OCRError("No text could be extracted from this PDF")
    return BankReportIn.model_validate({
        "source_id": source_id, "title": title, "report_date": report_date,
        "content_hash": content_hash, "original_filename": path.name,
        "fragments": fragments,
    }).model_dump()


__all__ = ["OCRError", "extract_bank_pdf"]
