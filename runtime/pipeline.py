# -*- coding: utf-8 -*-
"""Job-level orchestration shared by the Blender runner and the host tests."""
from __future__ import annotations

import json
import math

import numpy as np

import energy
import sdf

ENGINE = sdf.ENGINE


def _clean(v):
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        x = float(v)
        return x if math.isfinite(x) else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return v


def _knob_ranges(genome):
    out = {}
    for name, spec in (genome.get("knobs") or {}).items():
        if isinstance(spec, dict):
            out[name] = [float(spec.get("min", spec.get("value", 0.0) - 1.0)),
                         float(spec.get("max", spec.get("value", 0.0) + 1.0))]
        else:
            out[name] = [float(spec) - 1.0, float(spec) + 1.0]
    return out


def _snap(label, t, ctx):
    return {"label": label, "t": round(float(t), 4),
            "verts": ctx["verts"], "faces": ctx["faces"],
            "geometry": ctx["metrics"]}


def _grad_pack(g, tunables):
    return {"tunables": list(tunables),
            "total": g["total"],
            "terms": {k: {n: float(v.get(n, 0.0)) for n in tunables}
                      for k, v in g["terms"].items()}}


def _boundary_clipped(ctx):
    F = ctx["F"]
    sp = float(np.max(ctx["spacing"]))
    shell = [F[0, :, :], F[-1, :, :], F[:, 0, :], F[:, -1, :], F[:, :, 0], F[:, :, -1]]
    return bool(any(np.any(np.abs(s) < sp) for s in shell))


def run_job(job):
    op = str(job.get("op", "propose")).lower()
    if op not in ("propose", "energy", "refine"):
        raise ValueError("unknown op %r (propose|energy|refine)" % op)
    genome = job.get("genome")
    if not isinstance(genome, dict):
        raise ValueError("genome must be an object")
    if not genome.get("primitives"):
        raise ValueError("genome.primitives is required")
    terms = job.get("terms") or []
    if not isinstance(terms, list):
        raise ValueError("terms must be a list of energy-term objects")
    tunables = job.get("tunables")
    if tunables is None:
        tunables = list((genome.get("knobs") or {}).keys())
    tunables = [str(x) for x in tunables]
    unknown = [x for x in tunables if x not in (genome.get("knobs") or {})]
    if unknown:
        raise ValueError("unknown tunables: %s (declare them under knobs)" % ", ".join(unknown))
    seed = int(job.get("seed", 0) or 0)
    start = energy._jitter(genome, seed, float(job.get("jitter", 0.0) or 0.0))

    extra = {"op": op}
    if op == "refine":
        extra.update({"mode": job.get("mode", "soft"), "steps": int(job.get("steps", 8)),
                      "lr": float(job.get("lr", 0.08))})
    gh, lh = energy.hashes(start, terms, tunables, seed, extra=extra)

    base = {
        "ok": True,
        "engine": ENGINE,
        "op": op,
        "phase": "explore",
        "gate": {"mode": "none",
                 "why": "this layer reports energies; it never turns one into a block/pass verdict"},
        "genome_hash": gh,
        "lineage_hash": lh,
        "seed": seed,
        "tunables": tunables,
        "knob_ranges": _knob_ranges(start),
        "signals": [],
        "notes": [],
        "artifacts": {},
    }

    if op == "energy":
        t = float(job.get("t", 1.0))
        t = min(1.0, max(0.0, t))
        report, ctx = energy.analyze(start, terms, t=t)
        grad = None
        if job.get("gradient", True):
            grad = _grad_pack(energy.fd_gradient(start, terms, tunables, t=t, base=report), tunables)
        result = dict(base)
        result.update({"report": report, "gradient": grad, "genome": sdf.resolve_knobs(start),
                       "snapshots": [_snap("measured", t, ctx)["geometry"]]})
        result["signals"] = energy._signals(report, grad, tunables)
        result["notes"].append("t=%.2f: energy weights are the scheduled weights, not a judgement" % t)
        if _boundary_clipped(ctx):
            result["notes"].append("field touches grid bounds; the mesh is clipped open at that plane")
        if not terms:
            result["notes"].append("no energy terms supplied: pure geometry readout")
        return {"result": _clean(result), "snapshots": [_snap("measured", t, ctx)]}

    if op == "propose":
        t = float(job.get("t", 0.0))
        t = min(1.0, max(0.0, t))
        report, ctx = energy.analyze(start, terms, t=t)
        grad = None
        if job.get("gradient"):
            grad = _grad_pack(energy.fd_gradient(start, terms, tunables, t=t, base=report), tunables)
        result = dict(base)
        result.update({"report": report, "gradient": grad, "genome": sdf.resolve_knobs(start),
                       "signals": energy._signals(report, grad, tunables),
                       "snapshots": [_snap("proposal", t, ctx)["geometry"]]})
        result["notes"].append(
            "free proposal: energy weights are %.2f at t=%.2f; measurements are feedback, not permission"
            % (energy.ramp(t, {"start": 0.0, "end": 1.0}), t))
        if _boundary_clipped(ctx):
            result["notes"].append("field touches grid bounds; the mesh is clipped open at that plane")
        if not terms:
            result["notes"].append("no energy terms supplied: pure geometry proposal")
        return {"result": _clean(result), "snapshots": [_snap("proposal", t, ctx)]}

    # refine
    steps = int(job.get("steps", 8))
    lr = float(job.get("lr", 0.08))
    mode = str(job.get("mode", "soft"))
    rr = energy.refine(start, terms, tunables, steps=steps, lr=lr, mode=mode, seed=0)
    report = rr["final"]
    grad = None
    if job.get("gradient", True):
        gg = energy.fd_gradient(rr["final_genome_raw"], terms, tunables, t=1.0,
                                base=energy.analyze(rr["final_genome_raw"], terms, 1.0)[0])
        grad = _grad_pack(gg, tunables)
    result = dict(base)
    result.update({
        "report": report,
        "gradient": grad,
        "genome": sdf.resolve_knobs(start),
        "final_genome": rr["final_genome"],
        "final_genome_hash": energy.hashes(rr["final_genome"], terms, tunables, seed)[0],
        "mode": rr["mode"],
        "steps": rr["steps"],
        "lr": rr["lr"],
        "history": rr["history"],
        "signals": energy._signals(report, grad, tunables),
        "snapshots": [{"label": s["label"], "t": s["t"], "geometry": s["geometry"]} for s in rr["snapshots"]],
        "notes": [
            "refine ran after the free proposal; each step's effective weights are in history",
            "hard mode is included only as an A/B control: it applies all weight in one late step",
        ],
    })
    if _boundary_clipped(energy.build_context(sdf.resolve_knobs(start))):
        result["notes"].append("field touches grid bounds; the mesh is clipped open at that plane")
    result = _clean(result)
    return {"result": result, "snapshots": rr["snapshots"]}
