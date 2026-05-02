---
# workspace-wpbq
title: Filter null Lightener preferred state
status: completed
type: bug
priority: normal
created_at: 2026-05-02T20:52:23Z
updated_at: 2026-05-02T21:10:20Z
---

Lightener can restore or forward effect: None into child light.turn_on calls, causing Home Assistant service validation to reject first turn_on from off. Filter None-valued forwarded/preferred state while preserving real color/effect values.


## Work Plan

- [x] Add regression tests for null preferred state and explicit null clearing remembered effect.
- [x] Implement generic null filtering/clearing for forwarded preferred attributes.
- [x] Validate focused tests, full tests, and ruff.
- [x] Reassess docs/architecture impact.
- [x] Commit the completed change.


## Summary of Changes

Dropped `None` values from child `light.turn_on` service payloads and treated explicit null preferred-state attributes as a request to clear Lightener's remembered value. This prevents restored `effect: None` from reaching child lights while still allowing a later `effect=None` call to remove a previously remembered effect. No architecture or README update was needed; this narrows invalid service data handling inside the existing Lightener state model.

## Validation

- `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "ignores_none_preferred_state_values or none_clears_remembered_effect or restored_brightness or restore_data_keeps_last_brightness_when_off or does_not_resend_color_on_brightness_change"`
- `.venv/bin/python -m pytest`
- `.venv/bin/python -m ruff check custom_components/lightener/light.py tests/components/lightener/test_light.py`
