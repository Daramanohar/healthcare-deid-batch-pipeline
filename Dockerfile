FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY task1_pipeline ./task1_pipeline
COPY scripts ./scripts
COPY config ./config

ENTRYPOINT ["python", "-m", "task1_pipeline"]
CMD ["--config", "config/pipeline_config.json"]
