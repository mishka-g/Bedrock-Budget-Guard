FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir boto3
COPY seed.py .
CMD ["python", "seed.py"]
