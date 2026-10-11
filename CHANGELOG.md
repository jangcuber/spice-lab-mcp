# Changelog

## 1.3.0
- Licensed under GPL-3.0-or-later.
- Deterministic Probe control: `probe_add_traces`, `probe_set_axis`, `probe_mark_points`
  (menu commands by label path + dialog control IDs; no screen coordinates).
- Spec-based Capture schematics: `prepare_capture_spec`, `capture_part_reference`; per-pin net stubs,
  check-only `expect`, stub collision rejection, verification against Capture's generated netlist.
- Pin geometry confirmed by generated netlists (rotated R/C pin order, uA741 power pins).
- `set_window_layout` and `save_screenshot(crop=...)`.
- `capture_build_status` reports blocking Capture dialogs.
- Cadence auto-detection when `config.json` is absent; `config.example.json`; `install.ps1`.

## 1.2.0
- Guarded desktop input for Capture/Probe, real PSpice batch runs, `.PRINT`/Probe-export analysis,
  netlist templates, parameter batches, and constrained Tcl schematic templates.
