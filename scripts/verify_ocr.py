"""Run real Poppler/Tesseract extraction on temporary synthetic one-page PDFs."""

from pathlib import Path
from tempfile import TemporaryDirectory

from backend.ocr import extract_bank_pdf


def create_pdf(path: Path, text: str):
    stream = f"BT /F1 16 Tf 50 740 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


def main():
    with TemporaryDirectory(prefix="alfa-pdf-verification-") as directory:
        path = Path(directory) / "synthetic.pdf"
        # Short native text deliberately enters the raster/OCR branch.
        create_pdf(path, "BANK REPORT. Interest rate scenario.")
        report = extract_bank_pdf(path, source_id="SYNTHETIC_TEST", title="Synthetic OCR selftest", language="eng")
        text = report["fragments"][0]["text"].upper()
        if "BANK" not in text or report["fragments"][0]["verified"]:
            raise RuntimeError("Synthetic OCR extraction/verification state failed")
        print("PASS: real Poppler rasterization + Tesseract OCR; verification remains manual.")


if __name__ == "__main__":
    main()
