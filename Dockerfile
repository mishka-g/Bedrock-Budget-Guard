# Real-AWS / standalone image. Build from repo root:
#   docker build -t bedrock-budget-guard .
#   docker compose -f docker-compose.aws.yaml up -d --build
#
# Does not replace budget-guard/Dockerfile (local demo).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUDGET_GUARD_CONFIG=/app/config.yaml \
    BUDGET_GUARD_STATE=/app/state/state.json

WORKDIR /app

COPY budget-guard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /app/state

COPY budget-guard/*.py ./
COPY budget-guard/config.yaml ./

VOLUME ["/app/state"]

CMD ["python", "main.py"]
