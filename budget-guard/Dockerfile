# Local demo image (used by docker-compose.yaml → build: ./budget-guard).
# Keep this simple so Compose volumes and `make test` keep working.
# For real AWS, prefer the root Dockerfile: `docker build -t bedrock-budget-guard .`

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
