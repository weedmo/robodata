"""Spec-1 placeholder. Spec-3 replaces this with a real DB-queue consumer."""

import logging
import time


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("curation-worker")
    while True:
        log.info("placeholder: queue consumer not yet implemented (Spec-3)")
        time.sleep(60)


if __name__ == "__main__":
    main()
