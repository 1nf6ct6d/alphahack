FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system alfa && useradd --system --gid alfa --home-dir /app alfa

COPY requirements-ml.txt requirements-backend.txt requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements-backend.txt

COPY . .
RUN mkdir -p /app/data && chown -R alfa:alfa /app

USER alfa
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2).read()"

CMD ["python", "-m", "backend.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
