# -*- coding: utf-8 -*-
"""Soft constraint layer.

Every constraint is an energy term with a weight and a schedule.  Terms are
*measured*, never turned into a boolean verdict here.  The only hard decision
left is the caller's: when (and whether) to treat a number as a delivery gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

import numpy as np

import sdf

ENGINE = sdf.ENGINE


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
def ramp(t, schedule):
    """Smoothstep gate in [0,1].  Weight is exactly 0 before `start`."""
    if not schedule:
        return 1.0
    start = float(schedule.get("start", 0.0))
    end = float(schedule.get("end", 1.0))
    if end <= start:
        return 1.0 if t >= end else 0.0
    x = min(1.0, max(0.0, (float(t) - start) / (end - start)))
    return x * x * (3.0 - 2.0 * x)


# --------------------------------------------------------------------------- #
# analysis context
# --------------------------------------------------------------------------- #
def _diag(lo, hi):
    return float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))


def make_probes(resolved):
    """One fixed probe set, reused by every FD/refine evaluation.

    Without this, each perturbed field selects a slightly different shell
    sample, and the finite-difference gradient is dominated by sampling noise.
    """
    F, origin, spacing = sdf.grid_of(resolved)
    P, N = sdf.band_samples(F, origin, spacing)
    band = np.abs(F) <= 2.0 * float(np.max(spacing))
    return {"P": P, "N": N, "band_mask": band,
            "spacing": spacing, "origin": origin, "shape": F.shape}


def build_context(resolved, probes=None):
    F, origin, spacing = sdf.grid_of(resolved)
    verts, faces = sdf.surface_nets(F, origin, spacing)
    metrics = sdf.mesh_metrics(verts, faces)
    if probes is not None:
        P, N = probes["P"], probes["N"]
        band_mask = probes["band_mask"]
    else:
        P, N = sdf.band_samples(F, origin, spacing)
        band_mask = np.abs(F) <= 2.0 * float(np.max(spacing))
    lo, hi = sdf.bounds_of(resolved)
    ctx = {
        "F": F, "origin": origin, "spacing": spacing,
        "verts": verts, "faces": faces, "metrics": metrics,
        "P": P, "N": N, "band_mask": band_mask, "lo": np.asarray(lo), "hi": np.asarray(hi),
        "diag": _diag(lo, hi),
        "_thickness": None,
    }
    return ctx


def thickness_of(ctx, resolved, q=0.1):
    if ctx["_thickness"] is None:
        max_t = 0.8 * ctx["diag"]
        th, censored = sdf.ray_thickness(
            resolved, ctx["P"], ctx["N"], max_t, ctx["spacing"], steps=40)
        ctx["_thickness"] = (th, censored)
    th, censored = ctx["_thickness"]
    return float(np.percentile(th, max(0.0, min(100.0, q * 100.0)))), th, censored


# --------------------------------------------------------------------------- #
# one energy term
# --------------------------------------------------------------------------- #
def term_energy(cfg, resolved, ctx, t):
    tid = str(cfg.get("id", "")).lower()
    weight = float(cfg.get("weight", 1.0))
    eff = weight * ramp(t, cfg.get("schedule"))
    target_raw = cfg.get("target")
    if isinstance(target_raw, list):
        target = [float(x) for x in target_raw]
    elif target_raw is None:
        target = None
    else:
        target = float(target_raw)
    tol = float(cfg.get("tol", 0.05))
    q = float(cfg.get("q", 0.1))
    measured = None
    unit = ""
    raw = 0.0
    detail = {}

    if tid == "thickness":
        qv, th, censored = thickness_of(ctx, resolved, q)
        measured = qv
        unit = "mm"
        tg = target if target is not None else 2.0
        over = np.maximum(tg - th, 0.0)
        raw = float(np.mean(over * over)) / max(tg * tg, 1e-9)
        detail = {"p05": round(float(np.percentile(th, 5)), 3),
                  "p50": round(float(np.percentile(th, 50)), 3),
                  "p95": round(float(np.percentile(th, 95)), 3),
                  "censored_frac": round(float(np.mean(censored)), 3)}
        target = tg

    elif tid == "symmetry":
        axis = str(cfg.get("axis", "x")).lower()
        ax = {"x": 0, "y": 1, "z": 2}.get(axis, 0)
        center = float(cfg.get("center", 0.0))
        # Project the shell samples onto the zero level first, otherwise the
        # shell's own |f| (~1 grid cell) would be misread as asymmetry.
        f0 = sdf.build_field(resolved, ctx["P"])
        Pp = ctx["P"] - f0[:, None] * ctx["N"]
        Pm = Pp.copy()
        Pm[:, ax] = 2.0 * center - Pm[:, ax]
        fm = sdf.build_field(resolved, Pm)
        scale = max(1.0, 0.05 * ctx["diag"])
        raw = float(np.mean(fm * fm)) / (scale * scale)
        measured = round(float(np.sqrt(np.mean(fm * fm))), 3)
        unit = "mm (rms off-mirror)"
        target = 0.0
        detail = {"plane": axis, "center": center, "scale_mm": round(scale, 3)}

    elif tid == "curvature":
        sp = [float(s) for s in ctx["spacing"]]
        gx, gy, gz = np.gradient(ctx["F"], *sp, edge_order=1)
        n = np.stack([gx, gy, gz], axis=-1)
        ln = np.linalg.norm(n, axis=-1)
        ln[ln < 1e-12] = 1.0
        n = n / ln[..., None]
        # divergence of the unit normal = mean curvature (approx.)
        dx = np.gradient(n[..., 0], sp[0], axis=0, edge_order=1)
        dy = np.gradient(n[..., 1], sp[1], axis=1, edge_order=1)
        dz = np.gradient(n[..., 2], sp[2], axis=2, edge_order=1)
        H = dx + dy + dz
        band = ctx["band_mask"]
        L = max(1e-6, 0.10 * ctx["diag"])
        hv = H[band]
        raw = float(np.mean((hv * L) ** 2)) if hv.size else 0.0
        measured = round(float(np.mean(np.abs(hv))), 6) if hv.size else 0.0
        unit = "1/mm (mean |H|)"
        target = 0.0
        detail = {"H_times_L_rms": round(float(np.sqrt(raw)), 4), "L_mm": round(L, 3)}

    elif tid == "volume":
        v = float(ctx["metrics"]["volume_mm3"])
        measured = v
        unit = "mm^3"
        if target is None:
            raw = 0.0
        else:
            raw = (max(0.0, abs(v - target) - tol * abs(target)) / max(abs(target), 1e-9)) ** 2

    elif tid == "size":
        size = np.asarray(ctx["metrics"]["size_mm"], dtype=np.float64)
        measured = [round(float(x), 3) for x in size]
        unit = "mm (xyz)"
        if target is None:
            raw = 0.0
        else:
            tg = np.asarray(target, dtype=np.float64)
            raw = float(np.mean((np.maximum(np.abs(size - tg) - tol * tg, 0.0) / np.maximum(tg, 1e-9)) ** 2))

    elif tid == "closed":
        closed = bool(ctx["metrics"]["closed"])
        measured = 1 if closed else 0
        unit = "bool"
        target = 1.0
        raw = 0.0 if closed else 1.0

    elif tid == "components":
        c = int(ctx["metrics"]["components"])
        measured = c
        unit = "count"
        target = float(cfg.get("target", 1))
        raw = float(max(0.0, abs(c - target) - tol * max(target, 1.0)) ** 2)

    elif tid == "clearance":
        measured = None
        unit = "mm"
        raw = 0.0
        detail = {"state": "unresolved",
                  "why": "v0.1 measures single-field energies; multi-part clearance is not implemented"}

    else:
        measured = None
        unit = ""
        raw = 0.0
        detail = {"state": "unresolved", "why": "unknown term id %r" % tid}

    if measured is None:
        state = "unresolved"
    elif target is None:
        state = "measured"
    elif tid == "thickness":
        state = "ok" if measured >= target - tol * max(abs(target), 1e-9) else "below"
    elif tid in ("symmetry", "curvature"):
        # here tol is an absolute tolerance on the normalized raw energy
        state = "ok" if raw <= max(tol, 1e-9) else "above"
    elif isinstance(measured, list):
        state = "ok" if raw <= tol * tol else "above"
    elif measured < target - tol * max(abs(target), 1e-9):
        state = "below"
    elif measured > target + tol * max(abs(target), 1e-9):
        state = "above"
    else:
        state = "ok"

    return {
        "id": tid,
        "weight": weight,
        "effective_weight": round(eff, 6),
        "raw": round(float(raw), 6),
        "weighted": round(float(eff * raw), 6),
        "measured": [round(float(x), 3) for x in measured] if isinstance(measured, list) else (
            round(float(measured), 6) if isinstance(measured, (int, float)) else None),
        "unit": unit,
        "target": [round(float(x), 3) for x in target] if isinstance(target, list) else target,
        "tol": tol,
        "state": state,
        "schedule": cfg.get("schedule"),
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# full evaluation
# --------------------------------------------------------------------------- #
def analyze(genome, terms, t=1.0, probes=None):
    resolved = sdf.resolve_knobs(genome)
    ctx = build_context(resolved, probes=probes)
    rows = []
    total = 0.0
    for cfg in terms or []:
        row = term_energy(cfg, resolved, ctx, t)
        rows.append(row)
        total += float(row["weighted"])
    return {
        "t": round(float(t), 6),
        "total": round(float(total), 6),
        "terms": rows,
        "geometry": ctx["metrics"],
        "genome": resolved,
    }, ctx


def _clone(genome):
    return copy.deepcopy(genome)


def _knob_value(genome, name):
    spec = genome.get("knobs", {}).get(name)
    if isinstance(spec, dict):
        return float(spec.get("value", 0.0))
    if spec is None:
        raise ValueError("unknown tunable knob %r" % name)
    return float(spec)


def _knob_range(genome, name):
    spec = genome.get("knobs", {}).get(name)
    if not isinstance(spec, dict):
        v = float(spec) if spec is not None else 0.0
        return v - 1.0, v + 1.0
    v = float(spec.get("value", 0.0))
    lo = float(spec.get("min", v - 1.0))
    hi = float(spec.get("max", v + 1.0))
    return lo, hi


def fd_gradient(genome, terms, tunables, t=1.0, base=None, probes=None):
    """Central finite-difference gradient of the weighted total energy."""
    if base is None:
        base, _ = analyze(genome, terms, t, probes=probes)
    total_grad = {}
    per_term = {str(r["id"]): {} for r in base["terms"]}
    for name in tunables or []:
        p = _knob_value(genome, name)
        lo, hi = _knob_range(genome, name)
        h = max((hi - lo) * 0.002, 0.02)
        h = min(h, max(1e-3, (hi - lo) * 0.05))
        gp = _clone(genome)
        gm = _clone(genome)
        gp["knobs"][name]["value"] = min(hi, p + h)
        gm["knobs"][name]["value"] = max(lo, p - h)
        rp, _ = analyze(gp, terms, t, probes=probes)
        rm, _ = analyze(gm, terms, t, probes=probes)
        den = ((gp["knobs"][name]["value"] - gm["knobs"][name]["value"]) or 1.0)
        total_grad[name] = round((rp["total"] - rm["total"]) / den, 8)
        for i, row in enumerate(rp["terms"]):
            rid = str(row["id"])
            per_term.setdefault(rid, {})[name] = round(
                (row["weighted"] - rm["terms"][i]["weighted"]) / den, 8)
    return {"t": round(float(t), 6), "total": total_grad, "terms": per_term}


def _pick(x, max_points, seed=7):
    x = np.asarray(x, dtype=np.float64)
    if len(x) <= max_points:
        return x
    rng = np.random.default_rng(int(seed))
    return x[np.sort(rng.choice(len(x), int(max_points), replace=False))]


def field_drift(verts_a, genome_a, verts_b, genome_b, max_points=480):
    """Symmetric surface drift in mm, measured against the signed distances.

    This is far less sample-density-biased than a vertex-cloud Chamfer: the
    field gives the true distance from a point to the other zero level.
    """
    a = _pick(verts_a, max_points)
    b = _pick(verts_b, max_points)
    da = np.abs(sdf.build_field(genome_b, a))
    db = np.abs(sdf.build_field(genome_a, b))
    return round(0.5 * (float(np.mean(da)) + float(np.mean(db))), 4)


def chamfer(a, b, max_points=600, seed=7):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if len(a) == 0 or len(b) == 0:
        return None
    if a.shape == b.shape and np.array_equal(a, b):
        return 0.0

    def pick(x):
        if len(x) <= max_points:
            return x
        rng = np.random.default_rng(int(seed))
        return x[np.sort(rng.choice(len(x), int(max_points), replace=False))]

    a = pick(a)
    b = pick(b)
    d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)
    ab = float(np.mean(np.sqrt(np.min(d2, axis=1))))
    ba = float(np.mean(np.sqrt(np.min(d2, axis=0))))
    return round(0.5 * (ab + ba), 4)


def _jitter(genome, seed, amount):
    if not seed or amount <= 0:
        return genome
    rng = np.random.default_rng(int(seed))
    g = _clone(genome)
    for name, spec in (g.get("knobs") or {}).items():
        if not isinstance(spec, dict):
            continue
        lo = float(spec.get("min", spec.get("value", 0.0) - 1.0))
        hi = float(spec.get("max", spec.get("value", 0.0) + 1.0))
        v = float(spec.get("value", 0.0))
        v += rng.uniform(-amount, amount) * (hi - lo)
        spec["value"] = min(hi, max(lo, v))
    return g


def _suggestion(row, grad_row, tunables):
    rid = row["id"]
    if row["state"] == "unresolved":
        return None
    if row["state"] == "ok":
        return None
    top = ""
    if grad_row and tunables:
        pairs = [(abs(float(grad_row.get(n, 0.0))), n) for n in tunables]
        pairs.sort(reverse=True)
        top = pairs[0][1] if pairs else ""
    if rid == "thickness":
        return ("thickness %s=%s mm < target %s; %s"
                % ("p10", row["measured"], row["target"],
                   ("largest sensitivity: %s" % top) if top else "no tunable exposed"))
    if rid == "symmetry":
        return "surface is off its mirror plane (rms %s mm); mirror the asymmetric primitive or move it" % row["measured"]
    if rid == "curvature":
        return "curvature energy %s above tolerance; smooth the junction or accept it as a design feature" % row["raw"]
    if rid in ("volume", "size"):
        return "measured %s vs target %s; adjust %s" % (row["measured"], row["target"], top or "the exposed knobs")
    return None


def _signals(report, grad, tunables):
    out = []
    for row in report["terms"]:
        s = _suggestion(row, (grad or {}).get("terms", {}).get(str(row["id"])), tunables)
        if s:
            out.append({"term": row["id"], "state": row["state"], "measured": row["measured"],
                        "target": row["target"], "suggestion": s})
    return out


def _hash(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def hashes(genome, terms, tunables, seed, extra=None):
    resolved = sdf.resolve_knobs(genome)
    gh = _hash({"engine": ENGINE, "genome": resolved})
    lh = _hash({"engine": ENGINE, "genome": resolved, "terms": terms,
                "tunables": list(tunables or []), "seed": int(seed or 0),
                "extra": extra or {}})
    return gh, lh


# --------------------------------------------------------------------------- #
# refine: constraints negotiate after the shape exists
# --------------------------------------------------------------------------- #
def refine(genome, terms, tunables, steps=8, lr=0.08, mode="soft",
           snapshot_steps=(0, None, -1), seed=0):
    g = _jitter(genome, seed, float(genome.get("jitter", 0.0) or 0.0))
    steps = max(2, min(40, int(steps)))
    mode = str(mode or "soft").lower()
    terms = copy.deepcopy(terms or [])
    if mode == "hard":
        for cfg in terms:
            cfg["schedule"] = {"start": 0.98, "end": 1.0}
        lr = max(float(lr), 0.5)

    snaps_at = set()
    for s in snapshot_steps:
        idx = steps - 1 if s == -1 else (steps // 2 if s is None else int(s))
        if 0 <= idx < steps:
            snaps_at.add(idx)

    history = []
    snapshots = []
    probes = make_probes(sdf.resolve_knobs(g))
    report0, ctx0 = analyze(g, terms, t=0.0, probes=probes)
    prev_verts = ctx0["verts"]
    prev_res = sdf.resolve_knobs(g)
    if 0 in snaps_at:
        snapshots.append({"t": 0.0, "label": "free", "verts": ctx0["verts"], "faces": ctx0["faces"],
                          "geometry": ctx0["metrics"]})
    history.append({"step": 0, "t": 0.0, "total": report0["total"],
                    "terms": {r["id"]: r["measured"] for r in report0["terms"]},
                    "weights": {r["id"]: r["effective_weight"] for r in report0["terms"]},
                    "max_delta_norm": None, "chamfer_mm": None, "step_scale": None})

    final_report, final_ctx = report0, ctx0
    for i in range(1, steps):
        tt = (i / (steps - 1)) if steps > 1 else 1.0
        if mode == "hard":
            t = 1.0 if i == steps - 1 else 0.0
        else:
            t = tt
        base, ctx = analyze(g, terms, t=t, probes=probes)
        grad = fd_gradient(g, terms, tunables, t=t, base=base, probes=probes)

        # Per-term normalized direction: a term with a large raw scale (e.g.
        # curvature) must not drown out a term measured in millimetres.
        dirn = {n: 0.0 for n in tunables}
        for cfg in terms:
            eff = float(cfg.get("weight", 1.0)) * ramp(t, cfg.get("schedule"))
            if eff <= 0.0:
                continue
            gd = grad["terms"].get(str(cfg.get("id"))) or {}
            mx = max([abs(float(gd.get(n, 0.0))) for n in tunables] or [0.0])
            if mx <= 1e-15:
                continue
            for n in tunables:
                dirn[n] += eff * float(gd.get(n, 0.0)) / mx
        dmax = max([abs(v) for v in dirn.values()] or [0.0])

        max_norm = 0.0
        alpha_used = 0.0
        after, ctx2 = base, ctx
        if dmax > 1e-12:
            step = {}
            for n in tunables:
                lo, hi = _knob_range(g, n)
                step[n] = -float(lr) * (hi - lo) * dirn[n] / dmax

            if mode == "hard":
                trial = _clone(g)
                moved = 0.0
                for n in tunables:
                    lo, hi = _knob_range(trial, n)
                    p = _knob_value(trial, n)
                    q = min(hi, max(lo, p + step[n]))
                    moved = max(moved, abs(q - p) / max(hi - lo, 1e-9))
                    trial["knobs"][n]["value"] = q
                after, ctx2 = analyze(trial, terms, t=t, probes=probes)
                g = trial
                max_norm = moved
                alpha_used = 1.0
            else:
                alpha = 1.0
                accepted = None
                for _attempt in range(5):
                    trial = _clone(g)
                    moved = 0.0
                    for n in tunables:
                        lo, hi = _knob_range(trial, n)
                        p = _knob_value(trial, n)
                        q = min(hi, max(lo, p + alpha * step[n]))
                        moved = max(moved, abs(q - p) / max(hi - lo, 1e-9))
                        trial["knobs"][n]["value"] = q
                    tr, tctx = analyze(trial, terms, t=t, probes=probes)
                    if tr["total"] <= base["total"] + 1e-9:
                        accepted = (trial, tr, tctx, moved, alpha)
                        break
                    alpha *= 0.5
                if accepted is not None:
                    g, after, ctx2, max_norm, alpha_used = accepted
                # no accepted step -> stay put (still a valid, monotone step)

        if max_norm < 1e-9:
            ch = 0.0
        else:
            cur_res = sdf.resolve_knobs(g)
            ch = field_drift(prev_verts, prev_res, ctx2["verts"], cur_res)
            prev_verts = ctx2["verts"]
            prev_res = cur_res
        history.append({"step": i, "t": round(t, 4), "total": after["total"],
                        "terms": {r["id"]: r["measured"] for r in after["terms"]},
                        "weights": {r["id"]: r["effective_weight"] for r in after["terms"]},
                        "max_delta_norm": round(max_norm, 6), "chamfer_mm": ch,
                        "step_scale": round(alpha_used, 4)})
        if i in snaps_at:
            snapshots.append({"t": round(t, 4), "label": ("mid" if i != steps - 1 else "refined"),
                              "verts": ctx2["verts"], "faces": ctx2["faces"],
                              "geometry": ctx2["metrics"]})
        final_report, final_ctx = after, ctx2

    if not snapshots or snapshots[-1]["label"] != "refined":
        snapshots.append({"t": 1.0, "label": "refined", "verts": final_ctx["verts"],
                          "faces": final_ctx["faces"], "geometry": final_ctx["metrics"]})
    return {
        "mode": mode, "steps": steps, "lr": lr, "tunables": list(tunables or []),
        "history": history, "snapshots": snapshots,
        "final": final_report, "final_genome": sdf.resolve_knobs(g),
        "final_genome_raw": g,
    }
