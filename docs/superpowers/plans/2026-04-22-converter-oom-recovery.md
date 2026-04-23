# Converter OOM Recovery — Plan Notes

Memory guard rationale:
- `MEMORY_THRESHOLD_PCT` was lowered from `80` to `60` and the container memory limit was raised from `24g` to `48g` so the in-process guard can trip earlier under heavy conversion pressure instead of relying on a late kernel OOM kill at the old ceiling.
