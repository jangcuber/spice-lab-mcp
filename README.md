# spice-lab-mcp

A local [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI agent
(Claude Code, Codex, or any MCP client) drive **Cadence OrCAD Capture and PSpice A/D** on Windows:
draw schematics, run real simulations, mark points in Probe and save evidence screenshots, with
guard rails so the agent cannot run arbitrary scripts or touch anything outside the Cadence apps.

> **Not affiliated with Cadence Design Systems.** OrCAD, PSpice and Cadence are trademarks of
> Cadence Design Systems, Inc. This project contains **no Cadence software or libraries**; it
> automates a copy you have installed and licensed yourself. Check your Cadence license agreement
> (especially Lite/student editions) before using automation commercially.

[한국어 README](README.ko.md)

## What it can do

| Area | Tools |
|---|---|
| Environment / windows | `environment`, `list_windows`, `launch_app`, `activate_window`, `observe_window`, `set_window_layout` |
| Guarded desktop input | `click`, `drag`, `press_key`, `type_text`, `scroll`, `set_numeric_field` |
| Evidence | `save_screenshot` (native pixels, optional crop) |
| Probe without coordinates | `probe_add_traces`, `probe_set_axis`, `probe_mark_points` |
| Schematics from a JSON spec | `capture_part_reference`, `prepare_capture_spec` → `launch_capture_build` → `verify_capture_build` → `simulate_capture_build` |
| Built-in schematic templates | `prepare_capture` (RC, rectifier, clipper, R/C/L equivalence) |
| Netlists & real simulation | `find_files`, `read_circuit`, `create_netlist`, `list_templates`, `create_from_template`, `start_simulation`, `simulation_status`, `cancel_simulation` |
| Parameter batches | `start_parameter_batch`, `parameter_batch_status`, `cancel_parameter_batch` |
| Result data | `inspect_results`, `read_samples`, `measure_samples`, `compare_columns`, `read_probe_export`, `measure_probe_export` |

### Highlights

- **Schematics from a spec.** Describe whitelisted parts (R, C, L, D1N4148, VAC, VDC, VSIN, uA741)
  on a 0.1-inch grid, wires, net aliases and one analysis line. The server generates constrained
  Capture Tcl (nothing from the caller is evaluated), Capture draws it, and
  `verify_capture_build` compares **Capture's own generated netlist** with the nets you declared.
  Simulation is refused while anything mismatches.
- **Deterministic Probe control.** Menu commands are located by their visible label path at run time
  and sent as `WM_COMMAND`; dialogs are filled through fixed control IDs. Cursor searches such as
  `search forward level(max-3)` or `search forward min` drop Mark labels exactly, independent of
  window size or screen scaling.
- **Report-ready captures.** `set_window_layout(bounds)` fixes the window size so Probe's fixed-pixel
  labels stay readable when the screenshot is shrunk onto a page.

## Requirements

- Windows 10/11, unlocked interactive desktop session
- Cadence OrCAD Capture + PSpice A/D (tested with **17.2 Lite**; other versions may differ in pin
  geometry or menus — run the tests below)
- Python 3.10+ (3.12 tested)

## Install

```powershell
git clone <this repo> spice-lab-mcp
cd spice-lab-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1            # prints registration commands
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Register  # also registers with Claude Code
```

Cadence is auto-detected from `CADENCE_BIN`, `CDSROOT` or `C:\Cadence\SPB_*\tools\bin`.
To customise (read roots, timeouts), copy `config.example.json` to `config.json`.

Manual registration:

```powershell
claude mcp add --scope user spice-lab -- C:\path\to\spice-lab-mcp\.venv\Scripts\python.exe C:\path\to\spice-lab-mcp\server.py
codex mcp add spice-lab -- C:\path\to\spice-lab-mcp\.venv\Scripts\python.exe C:\path\to\spice-lab-mcp\server.py
```

Start a new client session afterwards so the tools are loaded.

## Example: a schematic from a spec

```json
{
  "parts": [
    {"ref": "VIN", "type": "VAC", "x": 0.6, "y": 1.6, "value": "1", "nets": {"-": "0"}, "expect": {"+": "VIN"}},
    {"ref": "R1",  "type": "R",   "x": 1.7, "y": 1.4, "value": "1k", "expect": {"1": "VIN", "2": "OUT"}},
    {"ref": "C1",  "type": "C",   "x": 2.2, "y": 1.8, "rotate": 90, "value": "100n", "nets": {"1": "0"}, "expect": {"2": "OUT"}}
  ],
  "wires":   [[0.7, 1.5, 1.6, 1.5], [2.0, 1.5, 2.3, 1.5], [2.3, 1.5, 2.3, 1.7], [2.3, 1.5, 2.8, 1.5]],
  "aliases": [[1.0, 1.5, "VIN"], [2.6, 1.5, "OUT"]],
  "analysis": [".AC DEC 100 1 100k"]
}
```

- `nets` draws a short stub from the pin with a net alias (or ground for `"0"`).
  `expect` only verifies a pin you wired yourself. `values` sets multi-property parts (e.g. VSIN).
- Stubs that would land in the middle of another wire, or overlap another stub, are rejected before
  anything is drawn (silent net merges were the most common real-world wiring bug).
- Pin offsets per rotation: `capture_part_reference`. Note that rotating R/C by 90° puts pin 1 at the
  **bottom**, and uA741 power pins end at (x+0.3, y+0) and (x+0.3, y+0.6).

A complete bootstrapped Twin-T notch filter (two uA741) is in `tests/verify_spec_probe.py`.

## Example: marking points in Probe

```
probe_add_traces(window_id, ["DB(V(VOUT))"])
probe_mark_points(window_id, ["search forward level(max-3)", "search forward min", "search forward level(max-3)"])
probe_set_axis(window_id, "x", "35", "100")
set_window_layout(window_id, "bounds", 0, 0, 1800, 1050)
save_screenshot(observation_id, "notch-response", crop=[0, 0, 1800, 860])
```

Probe computes `max`/`min` only over the **displayed** x range, so `probe_mark_points` resets X to
Auto first (`auto_x_first=True`); zoom afterwards. Probe's cursor grid is not machine-readable:
read the mark values from the returned screenshot and cross-check precise numbers with `.PRINT`
data (`measure_samples`).

## Safety model

- Only the configured `capture.exe` / `pspice.exe` windows can receive input; every input needs a fresh,
  single-use observation of an unoccluded, foreground window. System shortcuts are blocked; delete/close
  keys require explicit user confirmation.
- No terminal, console or Tcl typing. The only Tcl that runs is generated internally from validated
  numbers/names into a **new** project folder, verified unchanged before launch, and single-use.
- Netlists are ASCII-only, directive-whitelisted, and may reference only the configured `eval.lib`.
- Reads are limited to configured roots and file types; writes go only to new folders under `runtime/`.
- These are guard rails against mistakes, not a security sandbox against a malicious local caller.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"   # unit + guard tests (no GUI)
.\.venv\Scripts\python.exe tests\test_mcp.py                              # also checks the real stdio MCP protocol
.\.venv\Scripts\python.exe tests\test_mcp.py --live                       # runs real PSpice simulations
.\.venv\Scripts\python.exe tests\verify_extensions.py                     # 35 real template runs
.\.venv\Scripts\python.exe tests\verify_spec_probe.py                     # live: spec -> Capture (close Capture first)
.\.venv\Scripts\python.exe tests\verify_spec_probe.py --verify BUILD_ID   # live: netlist check -> PSpice -> Probe marks
```

## Known limitations

- Verified on Capture/PSpice 17.2 Lite only. Pin geometry for L, D1N4148 and VSIN comes from built-in
  templates and has not been re-verified through the spec path.
- If Capture was killed, its next start shows a "projects not properly closed" dialog that pauses the
  generated script; `capture_build_status` reports it as `blocking_dialogs`. Answer it, then prepare a new build.
- Builds are single-use by design; a failed build is never replayed automatically.
- Do not use the mouse/keyboard while the agent is sending input.

## License

Copyright (C) 2026 Jang cuber

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version (`GPL-3.0-or-later`). It is distributed WITHOUT ANY
WARRANTY. See [LICENSE](LICENSE) for the full text.

If you distribute modified versions, they must also be released under the GPL with source code.
Third-party dependencies (mcp, pywinauto, pywin32, comtypes, psutil, Pillow) keep their own
permissive licenses, which are GPL-compatible.
