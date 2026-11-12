#!/usr/bin/env -S uv run --with numpy,matplotlib --script
"""Pick a mirror-symmetric subset of the PCB's M2 holes that keeps keys and edges close to a screw.

The plate clamps the housings and dome flanges against the PCB, so where the screws go
decides how evenly every key is clamped. Candidates are the mounting holes on
caroline-pcb, paired by mirroring across the board's centre line. Every subset of N
pairs is tried (brute force; 21 choose 10 is ~350k) and ranked by

    score = RMS(key -> nearest screw) + W * RMS(outer edge point -> nearest screw)

RMS rather than max, because the worst key sits ~19-20 mm out for almost every subset and
stops telling them apart; RMS still punishes stragglers. The edge term keeps the outer
rim held down; raise W to favour it, 0 ignores it.

  uv run tools/pick_screws.py                          # 18 and 20 screws
  uv run tools/pick_screws.py --also keep:H1           # extra variant in the printout and plot
  uv run tools/pick_screws.py -n 20 --keep H5 --apply  # mark the best pick on the board

"Current" is the holes using the ring-silk footprint. --apply swaps holes between the
plain and ring-silk footprints, on the board and in the schematic, to match the best pick
of the first -n. Close the project in KiCad first.
"""
import argparse
import itertools
import math
import os
import re
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PCB_DIR = os.path.join(HERE, "..", "caroline-pcb")
PCB = os.path.join(PCB_DIR, "caroline-pcb.kicad_pcb")
SCH = os.path.join(PCB_DIR, "caroline-pcb.kicad_sch")
PLAIN = "MountingHole:MountingHole_2.2mm_M2"
RING = "caroline:MountingHole_2.2mm_M2_ring_silk"
KEY = "caroline:EC_Topre_1U_ViaSP"
EDGE_STEP = 2.0      # mm between sampled outline points
CUTOUT_R = 12.0      # outline points this close to a key centre are inner edges, not the rim
FAR = 15.0           # a key further than this from any screw counts as poorly held


def footprints(s):
    return re.split(r"(\n\t\(footprint )", s)


def at(f):
    m = re.search(r"\(at ([-\d.]+) ([-\d.]+)", f)
    return float(m[1]), float(m[2])


def load_holes():
    holes = {}
    for f in footprints(open(PCB).read())[2::2]:
        lib = f[1:f.index('"', 1)]
        if lib in (PLAIN, RING):
            ref = re.search(r'"Reference" "([^"]+)"', f)[1]
            holes[ref] = (*at(f), lib == RING)
    return holes


def load_keys():
    k = [at(f) for f in footprints(open(PCB).read())[2::2] if f.startswith(f'"{KEY}"')]
    assert len(k) == 36, len(k)
    return np.array(k)


def arc_points(a, m, b):
    # circle through three points, then sweep from a to b through m
    ax, ay = a; bx, by = b; mx, my = m
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    ux = ((ax**2 + ay**2) * (my - by) + (mx**2 + my**2) * (by - ay) + (bx**2 + by**2) * (ay - my)) / d
    uy = ((ax**2 + ay**2) * (bx - mx) + (mx**2 + my**2) * (ax - bx) + (bx**2 + by**2) * (mx - ax)) / d
    r = math.dist((ux, uy), a)
    t0, tm, t1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))
    sweep = (t1 - t0) % (2 * math.pi)
    if (tm - t0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    n = max(2, math.ceil(abs(sweep) * r / EDGE_STEP))
    return [(ux + r * math.cos(t0 + sweep * t), uy + r * math.sin(t0 + sweep * t)) for t in np.linspace(0, 1, n + 1)]


def load_outline():
    """Edge.Cuts as polylines."""
    s = open(PCB).read()
    lines = []
    for m in re.finditer(r"\n\t\((gr_line|gr_arc)\b(.*?)\n\t\)", s, re.S):
        body = m[2]
        if '(layer "Edge.Cuts")' not in body:
            continue
        pt = {k: tuple(map(float, re.search(rf"\({k} ([-\d.]+) ([-\d.]+)\)", body).groups()))
              for k in (("start", "end") if m[1] == "gr_line" else ("start", "mid", "end"))}
        lines.append([pt["start"], pt["end"]] if m[1] == "gr_line" else arc_points(pt["start"], pt["mid"], pt["end"]))
    assert lines, "no Edge.Cuts lines or arcs found"
    return lines


def load_edge(outline, keys):
    pts = []
    for line in outline:
        for a, b in zip(line, line[1:]):
            n = max(1, math.ceil(math.dist(a, b) / EDGE_STEP))
            for t in np.linspace(0, 1, n + 1):
                pts.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    pts = np.array(pts)
    near_key = np.min(np.linalg.norm(pts[:, None] - keys[None], axis=2), axis=1)
    return pts[near_key > CUTOUT_R]


def pair_up(holes):
    xs = [v[0] for v in holes.values()]
    cx = (min(xs) + max(xs)) / 2
    pairs, seen = [], set()
    for r, (x, y, _) in sorted(holes.items(), key=lambda kv: kv[1][0]):
        if r in seen:
            continue
        mate = min(holes, key=lambda q: math.hypot(holes[q][0] - (2 * cx - x), holes[q][1] - y))
        assert math.hypot(holes[mate][0] - (2 * cx - x), holes[mate][1] - y) < 0.05, (r, mate)
        seen |= {r, mate}
        pairs.append((r,) if mate == r else (r, mate))
    return pairs


def rms(d):
    return float(np.sqrt((d ** 2).mean()))


def measure(k, e, w):
    return dict(score=rms(k) + w * rms(e), krms=rms(k), kmax=float(k.max()), far=int((k > FAR).sum()), erms=rms(e))


def search(pairs, holes, keys, edge, n, keep, w):
    dk = {r: np.linalg.norm(keys - np.array(holes[r][:2]), axis=1) for r in holes}
    de = {r: np.linalg.norm(edge - np.array(holes[r][:2]), axis=1) for r in holes}
    forced = [p for p in pairs if keep & set(p)]
    free = [p for p in pairs if p not in forced]
    results = []
    for combo in itertools.combinations(free, n // 2 - len(forced)):
        refs = [r for p in forced + list(combo) for r in p]
        m = measure(np.min([dk[r] for r in refs], axis=0), np.min([de[r] for r in refs], axis=0), w)
        m["refs"] = sorted(refs, key=lambda r: int(r[1:]))
        results.append(m)
    results.sort(key=lambda m: m["score"])
    return results


def stats(refs, holes, keys, edge, w):
    p = np.array([holes[r][:2] for r in refs])
    k = np.min(np.linalg.norm(keys[:, None] - p[None], axis=2), axis=1)
    e = np.min(np.linalg.norm(edge[:, None] - p[None], axis=2), axis=1)
    return measure(k, e, w)


def fmt(m):
    return (f"score {m['score']:5.1f}  key rms {m['krms']:4.1f}  worst {m['kmax']:4.1f}  "
            f">{FAR:.0f}mm {m['far']:2d}  edge rms {m['erms']:4.1f}")


def plot(panels, holes, keys, edge, outline, w, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(len(panels), 1, figsize=(11, 4.6 * len(panels)))
    for ax, (title, refs) in zip(np.atleast_1d(axs), panels):
        for line in outline:
            xy = np.array(line)
            ax.plot(xy[:, 0], xy[:, 1], color="0.6", lw=0.7)
        allp = np.array([v[:2] for v in holes.values()])
        ax.scatter(allp[:, 0], allp[:, 1], s=30, facecolors="none", edgecolors="0.7", lw=0.8)
        if refs:
            p = np.array([holes[r][:2] for r in refs])
            ax.scatter(p[:, 0], p[:, 1], s=40, color="tab:red", zorder=3)
            for r in refs:
                ax.annotate(r, holes[r][:2], xytext=(3, 3), textcoords="offset points", fontsize=6)
            title = f"{title} ({len(refs)}): {fmt(stats(refs, holes, keys, edge, w))}"
        ax.set_title(title, fontsize=9)
        ax.set_aspect("equal")
        ax.invert_yaxis()
        ax.axis("off")
    plt.tight_layout()
    plt.savefig(out, dpi=90)


def ring(layer, flipped):
    # same silk circle as the library footprint; KiCad stores flipped footprints mirrored in y
    return (f"\t\t(fp_circle\n\t\t\t(center 0 0)\n\t\t\t(end 0 {'1.75' if flipped else '-1.75'})\n"
            f"\t\t\t(stroke\n\t\t\t\t(width 1)\n\t\t\t\t(type default)\n\t\t\t)\n\t\t\t(fill no)\n"
            f'\t\t\t(layer "{layer}")\n\t\t\t(uuid "{uuid.uuid4()}")\n\t\t)\n')


RING_RE = re.compile(r'\t\t\(fp_circle\n\t\t\t\(center 0 0\)\n\t\t\t\(end 0 -?1\.75\)\n.*?\n\t\t\)\n', re.S)


def apply(refs):
    parts = footprints(open(PCB).read())
    for i in range(2, len(parts), 2):
        f = parts[i]
        lib = f[1:f.index('"', 1)]
        if lib not in (PLAIN, RING):
            continue
        want = RING if re.search(r'"Reference" "([^"]+)"', f)[1] in refs else PLAIN
        if want == lib:
            continue
        f = f.replace(f'"{lib}"', f'"{want}"', 1)
        if want == RING:
            flipped = '(layer "B.Cu")' in f[:200]
            k = f.index("\t\t(fp_circle")
            f = f[:k] + ring("B.SilkS" if flipped else "F.SilkS", flipped) + f[k:]
        else:
            f = RING_RE.sub("", f, count=1)
        parts[i] = f
    open(PCB, "w").write("".join(parts))

    parts = re.split(r"(\n\t\(symbol\n)", open(SCH).read())
    for i in range(2, len(parts), 2):
        r = re.search(r'\(property "Reference" "(H\d+)"', parts[i])
        if r:
            want, other = (RING, PLAIN) if r[1] in refs else (PLAIN, RING)
            parts[i] = parts[i].replace(f'"{other}"', f'"{want}"', 1)
    open(SCH, "w").write("".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, nargs="+", default=[18, 20], help="screw counts to try (even)")
    ap.add_argument("-w", type=float, default=0.5, help="weight of the edge term (default 0.5)")
    ap.add_argument("--keep", default="", help="comma-separated refs that must stay (mirror is kept too)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--also", action="append", default=[], metavar="keep:H1,H5",
                    help="extra variant with more refs kept")
    ap.add_argument("--plot", default="pick_screws.png", help="where to write the plot")
    ap.add_argument("--apply", action="store_true", help="mark the best pick of the first -n on the board")
    a = ap.parse_args()

    holes = load_holes()
    keys = load_keys()
    outline = load_outline()
    edge = load_edge(outline, keys)
    current = sorted((r for r, v in holes.items() if v[2]), key=lambda r: int(r[1:]))
    pairs = pair_up(holes)
    keep = {r.strip() for r in a.keep.split(",") if r.strip()}

    if current:
        print(f"current {len(current):2d}: {fmt(stats(current, holes, keys, edge, a.w))}")
    print(f"candidates: {len(holes)} holes in {len(pairs)} mirror pairs, edge weight {a.w}\n")

    panels = [("current", current)]
    best = {}
    for n in a.n:
        res = search(pairs, holes, keys, edge, n, keep, a.w)
        best[n] = res[0]["refs"]
        panels.append((f"best {n}", best[n]))
        print(f"== {n} screws ({len(res)} subsets tried)")
        for m in res[:a.top]:
            print(f"  {fmt(m)}  {' '.join(m['refs'])}")
        print()
        for spec in a.also:
            if not spec.startswith("keep:"):
                raise SystemExit("bad --also spec: " + spec)
            v_keep = keep | {r.strip() for r in spec[5:].split(",") if r.strip()}
            res = search(pairs, holes, keys, edge, n, v_keep, a.w)
            panels.append((f"best {n}, {spec}", res[0]["refs"]))
            print(f"== {n} screws, {spec}")
            for m in res[:a.top]:
                print(f"  {fmt(m)}  {' '.join(m['refs'])}")
            print()

    plot(panels, holes, keys, edge, outline, a.w, a.plot)
    print("plot:", a.plot)

    if a.apply:
        refs = best[a.n[0]]
        apply(set(refs))
        print(f"applied: {len(refs)} ring-silk, {len(holes) - len(refs)} plain")


if __name__ == "__main__":
    main()
