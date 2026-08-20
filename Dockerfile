ARG ALLOW_NETWORK_BUILD=0
FROM python:3.12-slim-bookworm

ARG ALLOW_NETWORK_BUILD

# This image installs Playwright and Chromium. It is intentionally an
# explicit, network-enabled release operation; normal runtime and code-only
# rebuilds must use deploy/build-collector-offline.sh instead.
RUN test "$ALLOW_NETWORK_BUILD" = "1" || ( \
      echo "Refusing network dependency build. Use deploy/build-collector-offline.sh;" \
      echo "set ALLOW_NETWORK_BUILD=1 only for an intentional dependency/Chromium upgrade." >&2; \
      exit 42 \
    )

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN pip install --no-cache-dir "setuptools>=75" "playwright>=1.55,<2" \
    && playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 collector \
    && mkdir -p /data/raw \
    && chown collector:collector /data/raw

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --no-build-isolation .

USER collector

ENTRYPOINT ["job-market"]
CMD ["--help"]
