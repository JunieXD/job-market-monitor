FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN pip install --no-cache-dir "playwright>=1.55,<2" \
    && playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 collector \
    && mkdir -p /data/raw \
    && chown collector:collector /data/raw

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

USER collector

ENTRYPOINT ["job-market"]
CMD ["--help"]
