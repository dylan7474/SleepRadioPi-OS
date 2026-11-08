#!/usr/bin/env python3
"""Build the Aircraft scope's built-in map: app/sleepradiopi/data/map.json.

    tools/build_mapdata.py DIR [OUT]

DIR holds the public-domain sources (nothing here is downloaded for you; fetch them with curl):
  ne_10m_land.zip, ne_50m_land.zip   https://naturalearth.s3.amazonaws.com/{10m,50m}_physical/ne_{10m,50m}_land.zip
  ne_10m_populated_places_simple.zip https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_populated_places_simple.zip
  airports.csv                       https://davidmegginson.github.io/ourairports-data/airports.csv
Natural Earth (naturalearthdata.com) and OurAirports (ourairports.com) both put their data in the public domain.
The coast is kept in two layers (the world at 1:50m, and Europe in detail, 1:10m clipped to a box), simplified and
stored as whole numbers (degrees x q, each ring delta-coded). Towns and airfields are points. The radio draws it
itself, in the same projection as the scope's rings, so no map is ever fetched, and nothing is written to the card.
"""
from __future__ import annotations

import csv
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

EUROPE = (-26.0, 33.0, 46.0, 73.0)            # the detailed layer: west, south, east, north (degrees)
LAND50 = {"q": 100, "tol": 0.02}              # world, 1:50m: a hundredth of a degree, simplified to 0.02
LAND10 = {"q": 1000, "tol": 0.0035}           # Europe, 1:10m: a thousandth, simplified to ~400 m
TOWN_POP, TOWN_BOX = 25_000, (-12.0, 49.0, 4.0, 61.5)       # (towns: from this size, in this box: the islands and what's around them)
AIRPORT_BOX = (-14.0, 48.0, 8.0, 62.0)


def shp_polygons(data: bytes):
    """A shapefile of polygons -> [ring, ...], each ring [(lon, lat), ...] (outer and inner rings alike: filled even-odd)."""
    out, off = [], 100
    while off + 8 <= len(data):
        length = struct.unpack(">i", data[off + 4:off + 8])[0] * 2
        rec = data[off + 8:off + 8 + length]
        off += 8 + length
        if struct.unpack("<i", rec[:4])[0] != 5:
            continue
        nparts, npts = struct.unpack("<ii", rec[36:44])
        parts = list(struct.unpack(f"<{nparts}i", rec[44:44 + 4 * nparts])) + [npts]
        pts = struct.unpack(f"<{2 * npts}d", rec[44 + 4 * nparts:44 + 4 * nparts + 16 * npts])
        for a, b in zip(parts, parts[1:]):
            out.append([(pts[2 * i], pts[2 * i + 1]) for i in range(a, b)])
    return out


def rdp(pts: list, tol: float) -> list:
    """Douglas-Peucker, iterative (a coast has hundreds of thousands of points)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, at = 0.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * (x - x1) - dx * (y - y1)) / norm if norm else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > far:
                far, at = d, i
        if far > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


def clip(ring: list, box: tuple) -> list:
    """Sutherland-Hodgman against the box."""
    w, s, e, n = box
    for inside, cut in (
        (lambda p: p[0] >= w, lambda a, b: (w, a[1] + (b[1] - a[1]) * (w - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= e, lambda a, b: (e, a[1] + (b[1] - a[1]) * (e - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= s, lambda a, b: (a[0] + (b[0] - a[0]) * (s - a[1]) / (b[1] - a[1]), s)),
        (lambda p: p[1] <= n, lambda a, b: (a[0] + (b[0] - a[0]) * (n - a[1]) / (b[1] - a[1]), n)),
    ):
        out = []
        for i, cur in enumerate(ring):
            prev = ring[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
        ring = out
        if not ring:
            return []
    return ring


def pack(ring: list, q: int) -> list | None:
    """-> [west, south, east, north, x0, y0, dx, dy, ...] in whole numbers (degrees x q), or None if nothing is left."""
    pts, last = [], None
    for lon, lat in ring:
        p = (round(lon * q), round(lat * q))
        if p != last:
            pts.append(p)
            last = p
    if len(pts) < 3:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    flat = [min(xs), min(ys), max(xs), max(ys), pts[0][0], pts[0][1]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        flat += [x1 - x0, y1 - y0]
    return flat


def layer(rings: list, q: int, tol: float, box: tuple | None) -> list:
    out = []
    for r in rings:
        if box:
            r = clip(r, box)
        if len(r) < 3:
            continue
        r = rdp(r, tol)
        packed = pack(r, q)
        if packed:
            out.append(packed)
    return out


def dbf_rows(data: bytes):
    n, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields, off = [], 32
    while data[off] != 0x0D:
        fields.append((data[off:off + 11].split(b"\0")[0].decode(), data[off + 16]))
        off += 32
    for i in range(n):
        rec = data[hlen + i * rlen:hlen + (i + 1) * rlen]
        pos, row = 1, {}
        for name, width in fields:
            row[name] = rec[pos:pos + width].decode("utf-8", "replace").strip()
            pos += width
        yield row


def main(src: Path, out: Path) -> None:
    def shp(zipname: str, suffix: str) -> bytes:
        with zipfile.ZipFile(src / zipname) as z:
            return z.read(next(n for n in z.namelist() if n.endswith(suffix)))
    land50 = layer(shp_polygons(shp("ne_50m_land.zip", ".shp")), **LAND50, box=None)
    land10 = layer(shp_polygons(shp("ne_10m_land.zip", ".shp")), **LAND10, box=EUROPE)
    w, s, e, n = TOWN_BOX
    towns = []
    for r in dbf_rows(shp("ne_10m_populated_places_simple.zip", ".dbf")):
        try:
            lon, lat, pop = float(r["longitude"]), float(r["latitude"]), int(float(r["pop_max"] or 0))
        except (KeyError, ValueError):
            continue
        if w <= lon <= e and s <= lat <= n and pop >= TOWN_POP:
            towns.append([round(lon, 3), round(lat, 3), pop, r["name"]])
    towns.sort(key=lambda t: -t[2])
    airports = []
    w, s, e, n = AIRPORT_BOX
    with (src / "airports.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                lon, lat = float(r["longitude_deg"]), float(r["latitude_deg"])
            except ValueError:
                continue
            ident = r["ident"]
            big = r["type"] in ("large_airport", "medium_airport")
            small_uk = r["type"] == "small_airport" and r["iso_country"] in ("GB", "IE") and len(ident) == 4 and ident[:2] in ("EG", "EI")
            if w <= lon <= e and s <= lat <= n and (big or small_uk):
                airports.append([round(lon, 3), round(lat, 3), ident, r["name"][:40], 2 if r["type"] == "large_airport" else 1 if big else 0])
    doc = {"v": 1, "credit": "Natural Earth (public domain) and OurAirports (public domain)",
           "land50": {"q": LAND50["q"], "rings": land50},
           "land10": {"q": LAND10["q"], "box": list(EUROPE), "rings": land10},
           "towns": towns, "airports": airports}
    out.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    print(f"{out}: {out.stat().st_size / 1024:.0f} KB; land50 {len(land50)} rings, land10 {len(land10)} rings, {len(towns)} towns, {len(airports)} airfields")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2] if len(sys.argv) > 2 else "app/sleepradiopi/data/map.json"))
