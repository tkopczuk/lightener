---
# workspace-ip7d
title: Restore Lightener brightness and preserve Adaptive Lighting turn_on changes
status: completed
type: bug
priority: normal
created_at: 2026-05-02T19:56:57Z
updated_at: 2026-05-02T20:17:51Z
---

User expects first turn_on to use the last Lightener brightness, including after HA restart, and reports Adaptive Lighting brightness changes sent on Lightener turn_on are propagated to children but not preserved on the Lightener entity state.

## Checklist

- [x] Add regression for first turn_on using restored preferred brightness instead of inventing 100%.
- [x] Add regression proving preferred brightness is saved even after Lightener is off.
- [x] Add service-level Adaptive Lighting shaped regression using brightness_pct plus transition on the Lightener entity.
- [x] Implement RestoreEntity support and extra restore data for preferred brightness.
- [x] Validate focused, component, full-suite, lint, and adversarial checks.
- [x] Reassess architecture/docs impact.

## Validation

Focused restore/adaptive regressions passed after the restore implementation: `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "uses_restored_brightness or restore_data_keeps_last_brightness_when_off or preserves_adaptive_brightness or without_known_brightness_does_not_default_to_full"`. Component test file passed with 90 tests. Full suite passed with 124 tests. Ruff passed for `custom_components/lightener/light.py` and `tests/components/lightener/test_light.py` with only the existing Ruff config deprecation warning.

Adversarial checks covered an off-state Lightener whose normal brightness attribute is None but whose preferred brightness must still survive restart, restored first turn_on mapping through a 100:70 child brightness map, and an Adaptive Lighting shaped service call (`brightness_pct` plus `transition`) targeting the Lightener entity after it was already on.

Architecture reassessment: this adds Home Assistant RestoreEntity participation and a small Lightener-specific restore payload for preferred brightness. The public config schema and service surface do not change. The parent state model remains independent; child-only external adaptations still do not rewrite the Lightener parent brightness unless the external service targets the Lightener entity itself.

## Adaptive Lighting Interceptor Clarification

User provided Adaptive Lighting interceptor code. The first adaptation mutates the original `light.turn_on` service params in-place: it removes `entity_id`, preprocesses turn_on alternatives so values such as `brightness_pct` become raw `brightness`, then updates `data[CONF_PARAMS]` before Home Assistant calls the target entity. Added a direct entity-entry regression for that exact post-interceptor shape: Lightener is already on at raw 255, then receives `async_turn_on(brightness=102, transition=1)` and must store parent brightness 102 while mapping the child to raw 71.

Additional validation after this clarification: `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "intercepted_adaptive_brightness or preserves_adaptive_brightness or uses_restored_brightness"`, `.venv/bin/python -m pytest tests/components/lightener/test_light.py`, `.venv/bin/python -m pytest tests`, and `.venv/bin/python -m ruff check custom_components/lightener/light.py tests/components/lightener/test_light.py`. Full suite result: 125 passed.
