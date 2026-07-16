FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install \
    fastapi==0.139.1 uvicorn==0.51.0 jinja2==3.1.6 pynetbox==7.8.0 \
    pydantic==2.13.4 pydantic-settings==2.14.2 pyyaml==6.0.3 python-multipart==0.0.32

FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /srv
COPY --from=builder /install /usr/local
COPY app/ app/
COPY mappings.yaml ./

USER appuser
EXPOSE 8070
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8070/healthz', timeout=4)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8070"]
