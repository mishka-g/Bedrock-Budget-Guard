# Local demo image (used by docker-compose.yaml → build: ./budget-guard).
# Keep this simple so Compose volumes and `make test` keep working.
# For real AWS / Kubernetes, prefer the root Dockerfile:
#   docker build -t bedrock-budget-guard .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUDGET_GUARD_HTTP_PORT=8080

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

EXPOSE 8080

CMD ["python", "main.py"]
