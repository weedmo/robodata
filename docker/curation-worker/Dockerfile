FROM python:3.13-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir \
    asyncpg \
    numpy \
    pyarrow \
    pydantic \
    pydantic-settings
CMD ["python", "-m", "backend.workers.curation_worker"]
