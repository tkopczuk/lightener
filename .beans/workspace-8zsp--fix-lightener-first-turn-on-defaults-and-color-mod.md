---
# workspace-8zsp
title: Fix Lightener first turn-on defaults and color mode state write
status: completed
type: bug
priority: normal
created_at: 2026-05-02T19:46:53Z
updated_at: 2026-05-02T19:55:32Z
---

User reports the latest Lightener state/correction change defaults first turn_on to 100%, emits too many state writes, and can fail with unsupported color mode brightness for color-temp-only children. Remove the unconditional 255 fallback, avoid premature unsafe parent state writes, and keep child correction behavior covered.

## Checklist

- [x] Add regression for first plain turn_on not inventing full brightness.
- [x] Add regression for reducing parent writes when child state does not change.
- [x] Add regression for color-temp-only children not forcing unsupported brightness color mode.
- [x] Remove the unconditional 255 fallback and keep remembered/default-profile brightness mapping intact.
- [x] Replace premature parent state write with one post-service refresh/write.
- [x] Rework color mode fallback to choose an actually supported mode.
- [x] Run focused, component, full-suite, lint, and adversarial checks.
- [x] Reassess architecture/docs impact.

## Validation

Focused tests first failed on the old behavior for all three reported issues, then passed after the fix. Ran `.venv/bin/python -m pytest tests/components/lightener/test_light.py -k "without_known_brightness_does_not_default_to_full or writes_parent_state_once_without_child_updates or color_mode_unknown_uses_supported_child_mode or service_turn_on_maps_default_profile_brightness or service_turn_on_updates_parent_brightness_while_on"`, `.venv/bin/python -m pytest tests/components/lightener/test_light.py`, `.venv/bin/python -m pytest tests`, and `.venv/bin/python -m ruff check custom_components/lightener/light.py tests/components/lightener/test_light.py`. Full suite result: 121 passed.

Adversarial checks covered the old 255 fallback path with a 100:0 mapping, parent write count when child services do not emit state changes, and a color-temp-only child whose group color mode is still unknown. Existing regressions still cover HA default profiles and remembered brightness, so mapped brightness is still applied when Home Assistant or Lightener actually has a brightness value.

Architecture reassessment: this is an internal state reconciliation fix. It does not change config schema, services, entity ownership, or README-facing behavior. The parent state model remains independent; the only contract clarification is that first-ever turn_on without a brightness does not fabricate a Lightener brightness.
