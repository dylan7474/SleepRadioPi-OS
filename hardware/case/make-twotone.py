#!/usr/bin/env python3
"""Two-tone fronts: PrusaSlicer projects with the colour changes built in.

The lettered sunburst front (stl/front_logo_sunburst.stl) prints face down,
with SLEEP RADIO cut 0.8 mm into the face -- so the first 0.8 mm printed IS the
face, with letter-shaped holes, and whatever colour comes next shows at the
bottom of the letters. A pause for a filament change at the right layer
makes a two-tone front on a single-extruder printer:

  twotone/front_blue_face_white_letters.3mf   blue face, white letters, white
      panel: blue for the face, then white (one change, before the 1.0 mm layer)
  twotone/front_white_face_blue_letters.3mf   white face, blue letters, white
      panel: white for the face, blue for 4 layers (the bottom of the
      letters: one layer looked washed out), then white again (changes before
      the 1.0 and 1.8 mm layers) -- that 0.8 mm blue layer shows as a line
      round the panel's edge and in the grille slots

An STL can't hold a colour change, but a PrusaSlicer project can. These use
PrusaSlicer's own presets: Original Prusa i3 MK2.5, 0.20mm NORMAL @MK2.5 (so
the 0.8 mm face is exactly 4 layers) and Sunlu PLA. Open one, slice, print:
at each change (M600) the printer unloads, waits for the new filament, loads
it and asks on the LCD whether the colour is clear -- answer No to purge more.

    python3 make-twotone.py [--bundle ~/.config/PrusaSlicer/vendor/PrusaResearch.ini]

Needs prusa-slicer. It slices each project afterwards and checks each colour
change comes at the start of the right layer, before anything of it is printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONT = HERE / "stl" / "front_logo_sunburst.stl"
ROBERTS = HERE / "stl" / "front_roberts_sunburst.stl"   # "Roberts Radio" in Lobster (a radio for the user's dad)
TEST = HERE / "stl" / "twotone_test.stl"            # twotone_test.scad: a few minutes' print
OUT = HERE / "twotone"
PRESETS = {"printer": "Original Prusa i3 MK2.5", "print": "0.20mm NORMAL @MK2.5", "filament": "Sunlu PLA"}
BLUE, WHITE, BLACK = "#2F6FD6", "#F4F4F2", "#1C1C1C"
FACE_MM = 0.8                        # the letters' depth (sleepradiopi_box.scad: logo_depth)
LAYER_MM = 0.2                       # 0.20mm NORMAL: first layer and every layer
LETTER_LAYERS = 4                    # blue layers behind white-face letters: 1 looked washed out

# The colour changes: [(print_z of the first layer in the new colour, colour)]
BLUE_FACE = (BLUE, [(FACE_MM + LAYER_MM, WHITE)], "blue face, white letters")
WHITE_FACE = (WHITE, [(FACE_MM + LAYER_MM, BLUE), (FACE_MM + (LETTER_LAYERS + 1) * LAYER_MM, WHITE)], "white face, blue letters")
BLACK_LETTERS = (WHITE, [(FACE_MM + LAYER_MM, BLACK), (FACE_MM + (LETTER_LAYERS + 1) * LAYER_MM, WHITE)], "white face, black letters")
# name: (model, (first colour, changes, title)) -- a test tile of each, to try first
VARIANTS = {
    "front_blue_face_white_letters": (FRONT, BLUE_FACE),
    "front_white_face_blue_letters": (FRONT, WHITE_FACE),
    "roberts_white_face_blue_letters": (ROBERTS, WHITE_FACE),
    "roberts_white_face_black_letters": (ROBERTS, BLACK_LETTERS),
    "test_blue_face_white_letters": (TEST, BLUE_FACE),
    "test_white_face_blue_letters": (TEST, WHITE_FACE),
}


def read_bundle(path: Path) -> dict[str, dict[str, str]]:
    """Sections of a PrusaSlicer vendor bundle: {"print:Name": {key: value}}."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            current = sections.setdefault(m.group(1), {})
            continue
        if current is not None and "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return sections


def resolve(sections, kind: str, name: str, seen=()) -> dict[str, str]:
    """A preset with everything it inherits (parents in order, then its own)."""
    own = sections[f"{kind}:{name}"]
    out: dict[str, str] = {}
    for parent in [p.strip() for p in own.get("inherits", "").split(";") if p.strip()]:
        if parent in seen:
            raise ValueError(f"inheritance loop at {parent}")
        out.update(resolve(sections, kind, parent, seen + (name,)))
    out.update({k: v for k, v in own.items() if k != "inherits"})
    return out


def config(bundle: Path, first_colour: str) -> str:
    sections = read_bundle(bundle)
    merged: dict[str, str] = {}
    for kind, name in PRESETS.items():
        merged.update(resolve(sections, kind, name))
        merged[f"{kind}_settings_id"] = name
    for key in ("compatible_printers", "compatible_printers_condition", "compatible_prints",
                "compatible_prints_condition", "renamed_from"):
        merged.pop(key, None)
    merged["filament_colour"] = first_colour
    assert float(merged["layer_height"]) == LAYER_MM and float(merged["first_layer_height"]) == LAYER_MM, \
        "the print profile's layers aren't 0.2 mm: the colour changes would miss the face"
    return "".join(f"{k} = {v}\n" for k, v in sorted(merged.items()))


def colour_changes_xml(changes) -> str:
    # A colour change (M600) at the start of the layer at z, the first one in
    # the new colour: the printer unloads, waits for the new filament, loads
    # it and asks whether the colour is clear (No purges more).
    codes = "".join(f'<code print_z="{z:g}" type="0" extruder="1" color="{c}" extra="" '
                    f'gcode="M600"/>\n' for z, c in changes)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_print_z>\n'
            f'{codes}<mode value="SingleExtruder"/>\n</custom_gcodes_per_print_z>\n')


def build(name: str, bundle: Path, work: Path) -> Path:
    model, (first, changes, _) = VARIANTS[name]
    ini = work / f"{name}.ini"
    settings = config(bundle, first)
    ini.write_text(settings)
    project = OUT / f"{name}.3mf"
    subprocess.run(["prusa-slicer", "--load", str(ini), "--center", "125,105",       # the MK2.5 bed's middle
                    "--export-3mf", "--output", str(project), str(model)], check=True, capture_output=True)
    # Add the colour changes, and the settings: the command line saves only the
    # model, and a project without them opens on a generic printer.
    version = subprocess.run(["prusa-slicer", "--help"], capture_output=True, text=True).stdout.split()[0]
    embedded = f"; generated by {version}\n" + "".join(f"; {line}\n" for line in settings.splitlines())
    replaced = {"Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml", "Metadata/Slic3r_PE.config"}
    tmp = project.with_suffix(".tmp")
    with zipfile.ZipFile(project) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename not in replaced:
                dst.writestr(item, src.read(item.filename))
        dst.writestr("Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml", colour_changes_xml(changes))
        dst.writestr("Metadata/Slic3r_PE.config", embedded)
    tmp.replace(project)
    return project


def check(project: Path, changes, work: Path) -> tuple[list[float], str]:
    """Slice the project; return the Z of the layer each change is in -- it must
    come before anything of that layer is printed -- and the estimated time."""
    gcode = work / (project.stem + ".gcode")
    subprocess.run(["prusa-slicer", "--export-gcode", "--output", str(gcode), str(project)],
                   check=True, capture_output=True)
    found, estimate, layer, printed = [], "?", None, False
    for line in gcode.read_text().splitlines():
        if line.startswith("; estimated printing time (normal mode)"):
            estimate = line.split("=", 1)[1].strip()
        m = re.match(r";Z:([\d.]+)", line)
        if m:
            layer, printed = round(float(m.group(1)), 3), False
        elif line.startswith(";TYPE:"):             # the layer's first feature: printing has begun
            printed = True
        elif line.startswith("M600"):
            if printed:
                raise SystemExit(f"{project.name}: a colour change after the layer at {layer} mm had begun")
            found.append(layer)
    want = [round(z, 3) for z, _ in changes]
    if found != want:
        raise SystemExit(f"{project.name}: colour changes at the start of layers {found}, expected {want}")
    return found, estimate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bundle", type=Path,
                        default=Path.home() / ".config/PrusaSlicer/vendor/PrusaResearch.ini")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name, (model, (first, changes, title)) in VARIANTS.items():
            project = build(name, args.bundle, work)
            zs, estimate = check(project, changes, work)
            print(f"{project.relative_to(HERE)}: {title}; changes at the start of the layers at {zs} mm "
                  f"(checked); {estimate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
