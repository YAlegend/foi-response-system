# FOI response system — application image.
# Bundles semantic retrieval (fastembed/ONNX, CPU) and vendors the embedding
# model so retrieval works offline at runtime. The LLM runs in the separate
# `ollama` service (see docker-compose.yml), reached over HTTP — no model weights
# in this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FOI_EMBEDDING_CACHE_DIR=/app/models/fastembed

# libgomp1 is needed by onnxruntime (fastembed). poppler/tesseract are NOT
# installed: scanned-PDF OCR is out of scope; text PDFs use pypdf (pure Python).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Core deps + the optional semantic-retrieval stack (enabled in production).
RUN pip install -r requirements.txt \
 && pip install "fastembed==0.8.0" "numpy==2.4.6"

COPY app ./app
COPY sample_data ./sample_data

# Vendor the BGE-small embedding model into the image so semantic retrieval has
# no runtime download (set FOI_EMBEDDING_OFFLINE=true at runtime). Best-effort:
# if the build host is offline the model is fetched on first use instead.
RUN python -m app.fetch_model || true

EXPOSE 8000

# Single worker: SQLite is the default store and tolerates one writer. For
# multi-worker scale, switch FOI_DATABASE_URL to Postgres (see the deploy guide).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
