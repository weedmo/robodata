from backend.converter import queue_adapter
from backend.datasets.services import bronze_silver_pipeline as pipeline
from backend.workers import curation_worker


def test_bronze_silver_batch_is_claimed_by_converter_worker_only():
    assert "bronze_silver_batch" not in curation_worker.HANDLERS
    # The converter worker owns the filesystem-heavy bronze-to-silver batch.
    assert queue_adapter.HANDLERS["bronze_silver_batch"] is pipeline.handle_bronze_silver_batch
