FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev linux-libc-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p backend \
    && touch backend/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf backend

COPY backend /app/backend
COPY rosbag2lerobot-svt/nas /app/nas
COPY --chmod=0644 scripts/__init__.py /app/scripts/__init__.py
COPY --chmod=0644 scripts/recover_conversion.py /app/scripts/recover_conversion.py
COPY --chmod=0644 scripts/split_raw_task_by_metadata.py /app/scripts/split_raw_task_by_metadata.py

RUN chmod 0755 /app/scripts
RUN python -c "import nas.scanner; import backend.converter.service"

EXPOSE 8001

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]
