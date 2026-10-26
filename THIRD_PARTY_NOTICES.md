# Third party notices

This project depends on the following third-party Python packages. The package
versions used by the reproducible backend environment are pinned in
`requirements-backend.txt` and `requirements-ml.txt`.

| Package | License | Project |
| --- | --- | --- |
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| Pydantic | MIT | https://github.com/pydantic/pydantic |
| Uvicorn | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| tzdata | Apache-2.0 | https://github.com/python/tzdata |
| jsonschema | MIT | https://github.com/python-jsonschema/jsonschema |
| referencing | MIT | https://github.com/python-jsonschema/referencing |

Test-only dependency:

| Package | License | Project |
| --- | --- | --- |
| HTTPX | BSD-3-Clause | https://github.com/encode/httpx |

Each dependency remains subject to its own license and copyright notices. This
file is an inventory, not a replacement for the license text distributed with
each package.

The optional Docker OCR environment additionally installs Poppler (GPL-2.0-or-later)
and Tesseract OCR (Apache-2.0), with the Debian packages' own copyright and license
files available under `/usr/share/doc` inside the image. This source ZIP does not
redistribute those executables. The complete pinned Python inventory, including
transitive dependencies, is in `requirements.lock.txt`; installed distributions
retain their original license files.
