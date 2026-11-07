#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Headless selftest for the softfield engine (no bpy needed).

Run:  python dsh-blender-soft/tests/selftest.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "runtime"))

import numpy as np  # noqa: E402

import energy  # noqa: E402
import pipeline  # noqa: E402
import sdf  # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append({"check": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + ("  :: " + str(detail) if detail else ""))


def sphere_genome(r, res=64):
    return {"bounds": [[-40, -40, -40], [40, 40, 40]], "resolution": res,
            "primitives": [{"id": "s", "type": "sphere", "radius": float(r)}]}


def slab_genome(t, res=64):
    return {"bounds": [[-20, -20, -20], [20, 20, 20]], "resolution": res,
            "primitives": [{"id": "s", "type": "box", "size": [30.0, 30.0, float(t)]}]}


def capsule_genome(r, res=64):
    return {"bounds": [[-40, -30, -30], [40, 30, 30]], "resolution": res,
            "primitives": [{"id": "c", "type": "capsule", "a": [-20, 0, 0], "b": [20, 0, 0],
                            "radius": float(r)}]}


BRANCH = {
    "bounds": [[-75, -75, -75], [75, 75, 75]], "resolution": 64,
    "knobs": {
        "r_body": {"value": 26, "min": 14, "max": 36},
        "r_arm": {"value": 4.0, "min": 3, "max": 14},
        "spread": {"value": 26, "min": 8, "max": 42},
        "rise": {"value": 20, "min": -10, "max": 48},
        "r_hole": {"value": 7, "min": 2, "max": 14},
    },
    "primitives": [
        {"id": "body", "type": "sphere", "radius": "@r_body"},
        {"id": "armR", "type": "capsule", "a": [0, 0, 0], "b": ["@spread", 0, "@rise"],
         "radius": "@r_arm", "mirror": "x"},
        {"id": "hole", "type": "cylinder", "center": [0, 0, 0], "radius": "@r_hole",
         "height": 300},
    ],
    "build": [
        {"op": "union", "ids": ["body", "armR"], "k": 10},
        {"op": "subtract", "ids": ["hole"], "k": 4},
    ],
}

TERMS = [
    {"id": "thickness", "weight": 1.0, "target": 6.0, "q": 0.1,
     "schedule": {"start": 0.30, "end": 0.95}},
    {"id": "symmetry", "weight": 0.4, "axis": "x", "center": 0, "tol": 0.05,
     "schedule": {"start": 0.45, "end": 1.0}},
    {"id": "volume", "weight": 0.15, "target": 80000, "tol": 0.25,
     "schedule": {"start": 0.0, "end": 0.4}},
]


def mesh_of(g):
    F, o, sp = sdf.grid_of(g)
    return sdf.surface_nets(F, o, sp), F, o, sp


def main():
    t0 = time.time()
    print("softfield selftest · engine", sdf.ENGINE)
    print("python", sys.version.split()[0], "numpy", np.__version__)

    # 1. mesher
    (v, f), F, o, sp = mesh_of(sphere_genome(20.0))
    m = sdf.mesh_metrics(v, f)
    exact = 4.0 / 3.0 * math.pi * 20.0 ** 3
    check("mesher.sphere.closed_euler2", m["closed"] and m["euler"] == 2,
          f"closed={m['closed']} euler={m['euler']} verts={m['verts']}")
    check("mesher.sphere.volume_error", abs(m["volume_mm3"] - exact) / exact < 0.02,
          f"volume={m['volume_mm3']} exact={exact:.1f}")

    # 2. thickness
    for name, g, lo, hi in (
        ("sphere20", sphere_genome(20.0), 36.0, 44.0),
        ("slab4", slab_genome(4.0), 3.2, 4.2),
        ("capsule8", capsule_genome(8.0), 14.0, 17.0),
    ):
        F, origin, spacing = sdf.grid_of(g)
        P, N = sdf.band_samples(F, origin, spacing)
        th, censored = sdf.ray_thickness(g, P, N, 60.0, spacing, steps=40)
        med = float(np.median(th))
        check("thickness." + name, lo <= med <= hi,
              f"median={med:.2f} p05={np.percentile(th, 5):.2f} censored={int(censored.sum())}/{len(censored)}")

    # 3. symmetry term
    RB = sdf.resolve_knobs(BRANCH)
    F, origin, spacing = sdf.grid_of(RB)
    ctx = energy.build_context(RB)
    sym = energy.term_energy({"id": "symmetry", "axis": "x", "center": 0, "tol": 0.05},
                             RB, ctx, 1.0)
    check("energy.symmetry.mirrored", sym["state"] == "ok" and sym["measured"] < 0.6,
          f"rms={sym['measured']} state={sym['state']}")

    # 4. schedule
    check("schedule.zero_before_start", energy.ramp(0.2, {"start": 0.4, "end": 0.9}) == 0.0)
    check("schedule.one_at_end", energy.ramp(1.0, {"start": 0.4, "end": 0.9}) == 1.0)
    check("schedule.smooth_middle", 0.0 < energy.ramp(0.65, {"start": 0.4, "end": 0.9}) < 1.0)

    # 5. soft refine
    soft = pipeline.run_job({"op": "refine", "genome": BRANCH, "terms": TERMS,
                             "steps": 8, "lr": 0.08, "mode": "soft", "gradient": False})["result"]
    hist = soft["history"]
    deltas = [h["max_delta_norm"] for h in hist if isinstance(h.get("max_delta_norm"), (int, float))]
    drift = [h["chamfer_mm"] for h in hist if isinstance(h.get("chamfer_mm"), (int, float))]
    th0 = hist[0]["terms"]["thickness"]
    th1 = hist[-1]["terms"]["thickness"]
    vol = [t for t in soft["report"]["terms"] if t["id"] == "volume"][0]["measured"]
    check("soft.free_early", all(h["max_delta_norm"] == 0 for h in hist[1:3]),
          "first two moving attempts at t=0.14/0.29 must be zero")
    check("soft.step_bound", max(deltas) <= 0.0801, f"max_delta={max(deltas)}")
    check("soft.moves_later", sum(1 for d in deltas if d > 0) >= 3, f"moving_steps={sum(1 for d in deltas if d > 0)}")
    check("soft.thickness_improves", th1 > th0 + 1.0, f"{th0} -> {th1}")
    check("soft.volume_band", 60000 <= vol <= 100000, f"volume={vol}")
    check("soft.max_drift", max(drift) < 2.0, f"max_drift={max(drift)} mm")
    check("soft.no_verdict", (soft.get("gate") or {}).get("mode") == "none" and "verdict" not in soft)

    # 6. hard A/B control
    hard = pipeline.run_job({"op": "refine", "genome": BRANCH, "terms": TERMS,
                             "steps": 8, "lr": 0.08, "mode": "hard", "gradient": False})["result"]
    hh = hard["history"]
    moves = [h for h in hh[1:] if isinstance(h.get("max_delta_norm"), (int, float)) and h["max_delta_norm"] > 0]
    hvol = [t for t in hard["report"]["terms"] if t["id"] == "volume"][0]["measured"]
    check("hard.one_late_jump", len(moves) == 1 and moves[0]["max_delta_norm"] > 0.3,
          f"moves={len(moves)} delta={moves[0]['max_delta_norm'] if moves else None}")
    check("hard.large_drift", moves and moves[0]["chamfer_mm"] > 5.0,
          f"drift={moves[0]['chamfer_mm'] if moves else None} mm")
    check("hard.volume_overshoot", hvol > 120000, f"volume={hvol} (target band 60k-100k)")
    check("hard.worse_than_soft", hvol > vol * 1.3, f"hard={hvol} soft={vol}")

    # 7. hashes
    gh1, lh1 = energy.hashes(BRANCH, TERMS, ["r_body", "r_arm"], 0)
    gh2, lh2 = energy.hashes(BRANCH, TERMS, ["r_body", "r_arm"], 0)
    gh3, lh3 = energy.hashes(BRANCH, TERMS, ["r_body", "r_arm"], 7)
    check("hash.deterministic", gh1 == gh2 and lh1 == lh2, gh1[:12])
    check("hash.seed_changes_lineage_only", gh1 == gh3 and lh1 != lh3,
          f"genome={gh1 == gh3} lineage_changed={lh1 != lh3}")

    # 8. field drift metric
    (v1, _), _, _, _ = mesh_of(sphere_genome(20.0))
    (v2, _), _, _, _ = mesh_of(sphere_genome(20.5))
    d = energy.field_drift(v1, sdf.resolve_knobs(sphere_genome(20.0)),
                           v2, sdf.resolve_knobs(sphere_genome(20.5)))
    check("metric.field_drift_0p5mm", 0.2 < d < 1.0, f"drift={d} mm")

    ok = all(c["ok"] for c in CHECKS)
    out = {"ok": ok, "engine": sdf.ENGINE, "elapsed_s": round(time.time() - t0, 2),
           "passed": sum(1 for c in CHECKS if c["ok"]), "total": len(CHECKS), "checks": CHECKS}
    os.makedirs(os.path.join(ROOT, "..", "report"), exist_ok=True)
    with open(os.path.join(ROOT, "..", "report", "selftest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nRESULT", json.dumps({k: out[k] for k in ("ok", "passed", "total", "elapsed_s")}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
