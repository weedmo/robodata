FROM python:3.13-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .
RUN PYTHONPATH=/app/rosbag2lerobot-svt python -c "import av; from conversion.dataset_sync import sync_selected_episodes"
CMD ["python", "-m", "backend.workers.curation_worker"]
