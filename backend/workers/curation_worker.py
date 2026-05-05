"""Entry point for the unified curation-worker jobs consumer."""
from __future__ import annotations

import asyncio
import logging

from backend.datasets.services.cycle_stamp_handler import handle_stamp_cycles
from backend.datasets.services.delete_handler import handle_delete
from backend.datasets.services.merge_handler import handle_merge
from backend.datasets.services.split_handler import handle_split
from backend.datasets.services.sync_good_episodes_handler import handle_sync_good_episodes
from backend.workers.runtime import JobHandler, run_forever

HANDLERS: dict[str, JobHandler] = {
    "stamp_cycles": handle_stamp_cycles,
    "split": handle_split,
    "merge": handle_merge,
    "delete": handle_delete,
    "sync_good_episodes": handle_sync_good_episodes,
}


async def run_curation_worker_forever() -> None:  # pragma: no cover — ops entry point
    await run_forever(worker_id="curation-worker", handlers=HANDLERS)


def main() -> None:  # pragma: no cover — ops entry point
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_curation_worker_forever())


if __name__ == "__main__":  # pragma: no cover
    main()
