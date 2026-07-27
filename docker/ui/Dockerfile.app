FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY backend /app/backend
COPY rosbag2lerobot-svt/nas /app/nas

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev linux-libc-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir .
RUN python -c "import nas.scanner; import backend.converter.service"

EXPOSE 8001

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]
