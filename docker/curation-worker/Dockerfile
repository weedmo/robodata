FROM python:3.13-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .
RUN python -c "import av; from backend.datasets.services.dataset_ops_engine import _load_lerobot_dataset_tools; _load_lerobot_dataset_tools()"
RUN python -c "from backend.datasets.services.rosbag_dataset_sync import load_sync_selected_episodes; load_sync_selected_episodes()"
CMD ["python", "-m", "backend.workers.curation_worker"]
