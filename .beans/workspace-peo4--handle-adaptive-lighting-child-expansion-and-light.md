---
# workspace-peo4
title: Handle Adaptive Lighting child expansion and Lightener color state
status: completed
type: bug
priority: normal
created_at: 2026-05-02T20:33:04Z
updated_at: 2026-05-02T20:41:57Z
---

Adaptive Lighting expands a Lightener entity to its child light and targets the child directly. User reports moving Lightener brightness causes Adaptive Lighting adaptation cancellation/failure, and asks whether Lightener remembers color params in addition to brightness. Add explicit color state ownership/restore where appropriate and reconcile child-targeted adaptive updates without regressing independent parent state.


## Work Plan

- [x] Add tests for restored color state, no color resend on brightness-only change, and no child entity expansion.
- [x] Patch Lightener to own color restore state and hide child entity IDs from group expansion.
- [x] Validate focused tests and full Lightener test suite.
- [x] Run adversarial checks for stale restore data and Adaptive Lighting expansion behavior.
- [x] Reassess docs/architecture and update if needed.
- [x] Commit the completed change.


## Summary of Changes

Stored Lightener color/effect preferences alongside brightness, filtered restored state to persistent light attributes only, and stopped exposing controlled child entity IDs through the parent light state so Adaptive Lighting targets the Lightener entity directly. Added regression coverage for restored color, brightness-only changes, filtered stale restore data, and group expansion behavior. Updated README to document the direct-targeting behavior.

## Validation

- `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "restored_brightness or filters_restored_preferred_state or restore_data_keeps_last_brightness_when_off or does_not_resend_color_on_brightness_change or does_not_expose_child_entity_ids"`
- `.venv/bin/python -m pytest`
- `.venv/bin/python -m ruff check custom_components/lightener/light.py tests/components/lightener/test_light.py`
