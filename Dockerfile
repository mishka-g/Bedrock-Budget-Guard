# Real-AWS / Kubernetes image. Build from repo root:
#   docker build -t bedrock-budget-guard .
#   docker compose -f docker-compose.aws.yaml up -d --build
#
# Does not replace budget-guard/Dockerfile (local demo).
# No config is baked in — mount config.yaml (Compose) or a ConfigMap (K8s).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUDGET_GUARD_CONFIG=/app/config.yaml \
    BUDGET_GUARD_STATE=/app/state/state.json \
    BUDGET_GUARD_HTTP_PORT=8080

WORKDIR /app

# Pull current Debian security updates for OpenSSL (base image lags).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl3t64 \
        openssl \
        openssl-provider-legacy \
    && rm -rf /var/lib/apt/lists/*

COPY budget-guard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 65532 appuser \
    && useradd --uid 65532 --gid 65532 --system --home-dir /app --no-create-home appuser \
    && mkdir -p /app/state

COPY budget-guard/alert.py budget-guard/cost.py budget-guard/enforce.py \
     budget-guard/httpapi.py budget-guard/k8s_state.py budget-guard/leader.py \
     budget-guard/main.py budget-guard/metrics.py budget-guard/roles.py \
     budget-guard/state.py budget-guard/tracker.py ./

RUN chown -R 65532:65532 /app

USER 65532:65532

VOLUME ["/app/state"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "main.py"]
