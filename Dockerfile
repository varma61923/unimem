FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=30 \
    PIP_RETRIES=3

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY hub/requirements.txt hub/requirements-lock.txt ./hub/
RUN pip install --prefix=/install -r hub/requirements.txt -c hub/requirements-lock.txt \
    && pip install --target=/compiler-tools "Cython>=3.1,<4.0" setuptools==78.1.1 wheel

COPY hub/ /build/app/hub/
COPY protocol/ /build/app/protocol/
COPY unimem/ /build/app/unimem/
COPY scripts/compile_extensions.py /build/compile_extensions.py

RUN rm -rf /build/app/hub/tests \
    && PYTHONPATH=/compiler-tools python /build/compile_extensions.py compile \
        --root /build/app/hub \
        --root /build/app/unimem \
        --package-prefix hub \
        --package-prefix unimem \
        --strip-sources \
        --exclude-dir alembic \
        --keep-shim __init__.py \
        --keep-shim __main__.py \
        --keep-trampoline main.py \
        --keep-trampoline manage.py \
        --keep-trampoline smoke.py \
    && python /build/compile_extensions.py check-no-source \
        /build/app/hub /build/app/unimem \
        --exclude-dir alembic \
        --allow-shim __init__.py \
        --allow-shim __main__.py

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

# Apply fixes newer than the pinned base image (PCRE2 and OpenSSL).
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
        libpcre2-8-0 libssl3t64 openssl openssl-provider-legacy \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8420

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 10001 hub
WORKDIR /app

COPY --from=builder /build/app/hub/ /app/hub/
COPY --from=builder /build/app/protocol/ /app/protocol/
COPY --from=builder /build/app/unimem/ /app/unimem/

# Checkouts may use a private umask; the non-root service must read packaged artifacts.
RUN chmod -R u=rwX,go=rX /app

USER hub
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0) if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('HUB_PORT','8420')}/healthz\", timeout=2).status == 200 else sys.exit(1)"

CMD ["python", "-m", "hub.main"]
