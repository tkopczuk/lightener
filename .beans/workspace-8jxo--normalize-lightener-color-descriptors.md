---
# workspace-8jxo
title: Normalize Lightener color descriptors
status: completed
type: bug
priority: normal
created_at: 2026-05-02T22:53:16Z
updated_at: 2026-05-02T23:25:21Z
---

Lightener stores multiple derived color descriptors from restored or child-derived state and replays them together, causing Home Assistant light.turn_on schema validation to reject child service calls. Keep child payloads to one child-supported color descriptor while preserving effects and brightness behavior.


## Work Plan

- [x] Add regression tests for restored and explicit multi-descriptor color payloads.
- [x] Normalize preferred state and child service data to one color descriptor.
- [x] Validate focused tests, full tests, and ruff.
- [x] Reassess docs/architecture impact.
- [x] Commit the completed change.


## Summary of Changes

Normalized Lightener child `turn_on` payloads so each child receives at most one color descriptor, and only a descriptor supported by that child light `supported_color_modes`. Restored state can still retain multiple derived color descriptors, which lets mixed child devices receive the descriptor they can actually use. Explicit new color descriptors still replace the remembered color descriptor set, avoiding stale color state. No README update was needed because this is internal service payload sanitation.

## Validation

- `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "normalizes_restored_color_descriptors or drops_unsupported_color_descriptor or new_color_descriptor_clears_old_descriptor or restored_brightness"`
- `.venv/bin/python -m pytest`
- `.venv/bin/python -m ruff check custom_components/lightener/light.py tests/components/lightener/test_light.py`
