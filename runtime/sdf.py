# -*- coding: utf-8 -*-
"""SDF genome -> field -> surface-nets mesh.

This module is deliberately free of bpy: it runs under Blender's Python and
under a plain CPython with numpy.  A genome is the model's free proposal; the
field is the only geometry channel here.  Nothing in this file decides whether
a shape is "acceptable".
"""
from __future__ import annotations

import math

import numpy as np

ENGINE = "0.1.0"
GRID_MIN = 24
GRID_MAX = 128
DEFAULT_RES = 72


# --------------------------------------------------------------------------- #
# small math helpers
# --------------------------------------------------------------------------- #
def _vec(v, n=3, default=0.0):
    if v is None:
        return np.full(n, float(default))
    a = np.asarray(v, dtype=np.float64).reshape(-1)
    if a.size != n:
        raise ValueError("expected %d numbers, got %r" % (n, list(v)))
    return a


def _rot_matrix(rot_deg):
    rx, ry, rz = [math.radians(float(a)) for a in rot_deg]
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    return Rz @ Ry @ Rx


def _local(P, center, rotate):
    """World points -> primitive-local points (inverse rotation)."""
    d = P - center[None, :]
    rot = _vec(rotate) if rotate is not None else None
    if rot is not None and np.any(np.abs(rot) > 1e-12):
        d = d @ _rot_matrix(rot)  # (R^T d^T)^T
    return d


# --------------------------------------------------------------------------- #
# primitives (all return an approximate signed distance, negative inside)
# --------------------------------------------------------------------------- #
def primitive_sdf(prim, P):
    t = str(prim.get("type", "")).strip().lower()
    c = _vec(prim.get("center"), 3, 0.0)

    if t == "sphere":
        r = float(prim.get("radius", 1.0))
        return np.linalg.norm(P - c, axis=1) - r

    if t == "ellipsoid":
        r = np.maximum(_vec(prim.get("radii"), 3, 1.0), 1e-6)
        d = _local(P, c, prim.get("rotate"))
        k0 = np.linalg.norm(d / r, axis=1)
        k1 = np.linalg.norm(d / (r * r), axis=1)
        return np.where(k1 > 1e-12, k0 * (k0 - 1.0) / np.maximum(k1, 1e-12), k0 - 1.0)

    if t == "box":
        size = np.maximum(_vec(prim.get("size"), 3, 1.0), 1e-6)
        rnd = float(prim.get("round", 0.0))
        rnd = min(max(rnd, 0.0), float(size.min()) * 0.49)
        d = _local(P, c, prim.get("rotate"))
        q = np.abs(d) - (size * 0.5 - rnd)
        outside = np.linalg.norm(np.maximum(q, 0.0), axis=1)
        inside = np.minimum(np.max(q, axis=1), 0.0)
        return outside + inside - rnd

    if t == "capsule":
        a = _vec(prim.get("a"), 3, 0.0)
        b = _vec(prim.get("b"), 3, 1.0)
        r = float(prim.get("radius", 1.0))
        ba = b - a
        den = float(np.dot(ba, ba))
        if den < 1e-12:
            return np.linalg.norm(P - a, axis=1) - r
        h = np.clip(((P - a) @ ba) / den, 0.0, 1.0)
        closest = a[None, :] + h[:, None] * ba[None, :]
        return np.linalg.norm(P - closest, axis=1) - r

    if t == "cylinder":
        r = float(prim.get("radius", 1.0))
        h = float(prim.get("height", 1.0))
        d = _local(P, c, prim.get("rotate"))
        radial = np.linalg.norm(d[:, :2], axis=1)
        return np.maximum(radial - r, np.abs(d[:, 2]) - h * 0.5)

    if t == "torus":
        R = float(prim.get("major_radius", prim.get("R", 1.0)))
        r = float(prim.get("minor_radius", prim.get("r", 0.25)))
        d = _local(P, c, prim.get("rotate"))
        q = np.stack([np.linalg.norm(d[:, :2], axis=1) - R, d[:, 2]], axis=1)
        return np.linalg.norm(q, axis=1) - r

    if t == "plane":
        n = _vec(prim.get("normal"), 3, 0.0)
        if np.linalg.norm(n) < 1e-12:
            n = np.array([0.0, 0.0, 1.0])
        n = n / np.linalg.norm(n)
        return (P - c) @ n

    raise ValueError("unknown primitive type %r (sphere|ellipsoid|box|capsule|cylinder|torus|plane)" % t)


def _smin(a, b, k):
    if k is None or k <= 0:
        return np.minimum(a, b)
    h = np.maximum(float(k) - np.abs(a - b), 0.0) / float(k)
    return np.minimum(a, b) - h * h * (float(k) * 0.25)


def _smax(a, b, k):
    if k is None or k <= 0:
        return np.maximum(a, b)
    return -_smin(-a, -b, k)


# --------------------------------------------------------------------------- #
# genome -> field
# --------------------------------------------------------------------------- #
def resolve_knobs(genome):
    """Return a deep copy with '@name' strings replaced by knob values."""
    knobs = genome.get("knobs") or {}
    table = {}
    for name, spec in knobs.items():
        if isinstance(spec, dict):
            table[name] = float(spec.get("value", 0.0))
        else:
            table[name] = float(spec)

    def walk(v):
        if isinstance(v, str) and v.startswith("@"):
            key = v[1:]
            if key not in table:
                raise ValueError("unknown knob %r (declare it under knobs)" % key)
            return table[key]
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    out = {k: walk(v) for k, v in genome.items() if k != "knobs"}
    out["knobs"] = {k: float(v) for k, v in table.items()}
    return out


def _build_chunk(genome, P):
    prims = genome.get("primitives") or []
    if not prims:
        raise ValueError("genome.primitives is empty")
    by_id = {}
    for i, p in enumerate(prims):
        pid = str(p.get("id") or "p%d" % i)
        if pid in by_id:
            raise ValueError("duplicate primitive id %r" % pid)
        by_id[pid] = p
    cache = {}

    def field_of(pid):
        if pid not in by_id:
            raise ValueError("build references unknown primitive %r" % pid)
        if pid not in cache:
            p = by_id[pid]
            f = primitive_sdf(p, P)
            mir = p.get("mirror")
            if mir:
                axis = {"x": 0, "y": 1, "z": 2}.get(str(mir).lower(), 0)
                if isinstance(mir, dict):
                    axis = {"x": 0, "y": 1, "z": 2}.get(str(mir.get("axis", "x")).lower(), 0)
                    center = float(mir.get("center", 0.0))
                else:
                    center = float(p.get("mirror_center", 0.0))
                P2 = P.copy()
                P2[:, axis] = 2.0 * center - P2[:, axis]
                f = _smin(f, primitive_sdf(p, P2), float(p.get("mirror_k", 0.0)))
            cache[pid] = f
        return cache[pid]

    build = genome.get("build")
    if not build:
        build = [{"op": "union", "ids": list(by_id.keys()), "k": genome.get("blend", 0.0)}]

    acc = None
    for step in build:
        op = str(step.get("op", "union")).lower()
        k = float(step.get("k", 0.0) or 0.0)
        for pid in step.get("ids") or []:
            f = field_of(pid)
            if acc is None:
                acc = f
            elif op == "union":
                acc = _smin(acc, f, k)
            elif op == "subtract":
                acc = _smax(acc, -f, k)
            elif op == "intersect":
                acc = _smax(acc, f, k)
            else:
                raise ValueError("unknown build op %r (union|subtract|intersect)" % op)
    if acc is None:
        raise ValueError("genome.build produced no field")
    return acc


def build_field(genome, points, chunk=180_000):
    P = np.asarray(points, dtype=np.float64)
    if P.ndim != 2 or P.shape[1] != 3:
        raise ValueError("points must be (N,3)")
    out = np.empty(P.shape[0], dtype=np.float64)
    for s in range(0, P.shape[0], chunk):
        out[s:s + chunk] = _build_chunk(genome, P[s:s + chunk])
    return out


def _primitive_bbox(p):
    t = str(p.get("type", "")).lower()
    c = _vec(p.get("center"), 3, 0.0)
    if t == "sphere":
        r = float(p.get("radius", 1.0))
        return c - r, c + r
    if t == "ellipsoid":
        r = _vec(p.get("radii"), 3, 1.0)
        return c - r, c + r
    if t == "box":
        half = _vec(p.get("size"), 3, 1.0) * 0.5
        return c - half - 1.0, c + half + 1.0
    if t == "capsule":
        a, b = _vec(p.get("a"), 3, 0.0), _vec(p.get("b"), 3, 1.0)
        r = float(p.get("radius", 1.0))
        return np.minimum(a, b) - r, np.maximum(a, b) + r
    if t == "cylinder":
        r = float(p.get("radius", 1.0))
        h = float(p.get("height", 1.0))
        ext = np.array([r, r, h * 0.5])
        return c - ext, c + ext
    if t == "torus":
        R = float(p.get("major_radius", p.get("R", 1.0)))
        r = float(p.get("minor_radius", p.get("r", 0.25)))
        ext = np.array([R + r, R + r, r])
        return c - ext, c + ext
    raise ValueError("cannot derive bbox for primitive type %r" % t)


def bounds_of(genome):
    if genome.get("bounds") is not None:
        b = np.asarray(genome["bounds"], dtype=np.float64)
        if b.shape != (2, 3):
            raise ValueError("bounds must be [[xmin,ymin,zmin],[xmax,ymax,zmax]]")
        return b[0].copy(), b[1].copy()
    prims = genome.get("primitives") or []
    if not prims:
        raise ValueError("genome.primitives is empty")
    los, his = [], []
    for p in prims:
        lo, hi = _primitive_bbox(p)
        mir = p.get("mirror")
        if mir:
            axis = {"x": 0, "y": 1, "z": 2}.get(str(mir).lower(), 0)
            if isinstance(mir, dict):
                axis = {"x": 0, "y": 1, "z": 2}.get(str(mir.get("axis", "x")).lower(), 0)
                center = float(mir.get("center", 0.0))
            else:
                center = float(p.get("mirror_center", 0.0))
            lo[axis] = min(lo[axis], 2.0 * center - hi[axis])
            hi[axis] = max(hi[axis], 2.0 * center - lo[axis])
        los.append(lo)
        his.append(hi)
    lo, hi = np.min(los, axis=0), np.max(his, axis=0)
    span = float(np.max(hi - lo))
    margin = max(span * 0.08, 2.0)
    return lo - margin, hi + margin


def grid_of(genome):
    lo, hi = bounds_of(genome)
    res = genome.get("resolution", DEFAULT_RES)
    if isinstance(res, (int, float)):
        res = [int(res)] * 3
    res = [int(max(GRID_MIN, min(GRID_MAX, r))) for r in res]
    if hi[0] <= lo[0] or hi[1] <= lo[1] or hi[2] <= lo[2]:
        raise ValueError("degenerate bounds")
    axes = [np.linspace(lo[i], hi[i], res[i]) for i in range(3)]
    X, Y, Z = np.meshgrid(axes[0], axes[1], axes[2], indexing="ij")
    P = np.stack([X.ravel(), Y.ravel(), Z.ravel()], axis=1)
    F = build_field(genome, P).reshape(res)
    spacing = (hi - lo) / np.maximum(np.asarray(res) - 1, 1)
    return F, lo, spacing


# --------------------------------------------------------------------------- #
# surface nets
# --------------------------------------------------------------------------- #
_CORNERS = np.array(
    [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0),
     (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1)], dtype=np.int64)
_EDGES = [(0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3), (2, 6), (3, 7),
          (4, 5), (4, 6), (5, 7), (6, 7)]


def surface_nets(F, origin, spacing):
    F = np.asarray(F, dtype=np.float64)
    if F.ndim != 3 or min(F.shape) < 3:
        raise ValueError("F must be a 3D grid with at least 3 samples per axis")
    nx, ny, nz = F.shape
    inside = F < 0.0

    # active cells: at least one corner inside and one outside
    mn = np.minimum.reduce([F[a:nx - 1 + a, b:ny - 1 + b, c:nz - 1 + c]
                            for a, b, c in _CORNERS])
    mx = np.maximum.reduce([F[a:nx - 1 + a, b:ny - 1 + b, c:nz - 1 + c]
                            for a, b, c in _CORNERS])
    active = (mn < 0.0) & (mx >= 0.0)
    idx = np.argwhere(active)
    if idx.shape[0] == 0:
        raise ValueError("field has no zero crossing inside bounds; widen bounds or move the shape")

    acc = np.zeros((idx.shape[0], 3), dtype=np.float64)
    cnt = np.zeros(idx.shape[0], dtype=np.float64)
    for a, b in _EDGES:
        ca, cb = _CORNERS[a], _CORNERS[b]
        fa = F[idx[:, 0] + ca[0], idx[:, 1] + ca[1], idx[:, 2] + ca[2]]
        fb = F[idx[:, 0] + cb[0], idx[:, 1] + cb[1], idx[:, 2] + cb[2]]
        cross = (fa < 0.0) != (fb < 0.0)
        if not np.any(cross):
            continue
        den = fa - fb
        t = np.zeros_like(fa)
        ok = cross & (np.abs(den) > 1e-15)
        t[ok] = fa[ok] / den[ok]
        pos = ca[None, :] + t[:, None] * (cb - ca)[None, :]
        acc[cross] += pos[cross]
        cnt[cross] += 1.0

    local = acc / np.maximum(cnt, 1.0)[:, None]
    verts = origin[None, :] + (idx.astype(np.float64) + local) * spacing[None, :]

    cell_vid = np.full((nx - 1, ny - 1, nz - 1), -1, dtype=np.int64)
    cell_vid[idx[:, 0], idx[:, 1], idx[:, 2]] = np.arange(idx.shape[0], dtype=np.int64)

    quads = []

    # x-directed grid edges: cells (i, j-1, k-1) (i, j, k-1) (i, j, k) (i, j-1, k)
    f0 = F[:-1, 1:-1, 1:-1]
    f1 = F[1:, 1:-1, 1:-1]
    i, j, k = np.nonzero((f0 < 0.0) != (f1 < 0.0))
    if i.size:
        j = j + 1
        k = k + 1
        q = np.stack([cell_vid[i, j - 1, k - 1], cell_vid[i, j, k - 1],
                      cell_vid[i, j, k], cell_vid[i, j - 1, k]], axis=1)
        quads.append(q)

    # y-directed: cells (i-1, j, k-1) (i, j, k-1) (i, j, k) (i-1, j, k)
    f0 = F[1:-1, :-1, 1:-1]
    f1 = F[1:-1, 1:, 1:-1]
    i, j, k = np.nonzero((f0 < 0.0) != (f1 < 0.0))
    if i.size:
        i = i + 1
        k = k + 1
        q = np.stack([cell_vid[i - 1, j, k - 1], cell_vid[i, j, k - 1],
                      cell_vid[i, j, k], cell_vid[i - 1, j, k]], axis=1)
        quads.append(q)

    # z-directed: cells (i-1, j-1, k) (i, j-1, k) (i, j, k) (i-1, j, k)
    f0 = F[1:-1, 1:-1, :-1]
    f1 = F[1:-1, 1:-1, 1:]
    i, j, k = np.nonzero((f0 < 0.0) != (f1 < 0.0))
    if i.size:
        i = i + 1
        j = j + 1
        q = np.stack([cell_vid[i - 1, j - 1, k], cell_vid[i, j - 1, k],
                      cell_vid[i, j, k], cell_vid[i - 1, j, k]], axis=1)
        quads.append(q)

    if not quads:
        raise ValueError("surface nets produced no faces")
    faces = np.concatenate(quads, axis=0)
    good = np.all(faces >= 0, axis=1)
    faces = faces[good]
    if faces.shape[0] == 0:
        raise ValueError("surface nets produced no valid faces")

    faces = _orient_by_gradient(verts, faces, F, origin, spacing)
    verts, faces = _orient_outward(verts, faces)
    return verts, faces


def _orient_by_gradient(verts, faces, F, origin, spacing):
    """Flip faces against the field gradient (outward = increasing F).

    The per-edge axis test is unreliable wherever the surface is nearly
    parallel to that axis (e.g. a hole rim), which shows up as black flipped
    facets in the render.  The gradient at each quad centre is unambiguous.
    """
    gx, gy, gz = np.gradient(F, float(spacing[0]), float(spacing[1]), float(spacing[2]),
                            edge_order=1)
    centers = verts[faces].mean(axis=1)
    idx = np.rint((centers - origin[None, :]) / spacing[None, :]).astype(np.int64)
    for ax in range(3):
        idx[:, ax] = np.clip(idx[:, ax], 0, F.shape[ax] - 1)
    grad = np.stack([gx[idx[:, 0], idx[:, 1], idx[:, 2]],
                     gy[idx[:, 0], idx[:, 1], idx[:, 2]],
                     gz[idx[:, 0], idx[:, 1], idx[:, 2]]], axis=1)
    a = verts[faces[:, 0]]
    b = verts[faces[:, 1]]
    c = verts[faces[:, 2]]
    n = np.cross(b - a, c - a)
    flip = np.einsum("ij,ij->i", n, grad) < 0.0
    if np.any(flip):
        faces = faces.copy()
        faces[flip] = faces[flip][:, ::-1]
    return faces


def _triangles(faces):
    """Fan-triangulate quads (and pass through triangles)."""
    tris = []
    for f in faces:
        f = list(f)
        for j in range(1, len(f) - 1):
            tris.append((f[0], f[j], f[j + 1]))
    return np.asarray(tris, dtype=np.int64)


def _orient_block(verts, quads, lower_f, axis):
    """Flip each quad so its axis-component normal points outward.

    `lower_f` is the field value at the lower corner of the grid edge; the
    outward direction along `axis` is +1 when that corner is inside (<0).
    """
    if quads.size == 0:
        return quads
    a = verts[quads[:, 0]]
    b = verts[quads[:, 1]]
    c = verts[quads[:, 2]]
    n = np.cross(b - a, c - a)
    want = np.where(lower_f < 0.0, 1.0, -1.0)
    flip = np.sign(n[:, axis]) != want
    if np.any(flip):
        quads = quads.copy()
        quads[flip] = quads[flip][:, ::-1]
    return quads


def _signed_volume(verts, faces):
    tris = _triangles(faces)
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    return float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0)


def _orient_outward(verts, faces):
    if _signed_volume(verts, faces) < 0.0:
        faces = faces[:, ::-1].copy()
    return verts, faces


def vertex_normals(verts, faces):
    n = np.zeros_like(verts)
    tris = _triangles(faces)
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    fn = np.cross(b - a, c - a)
    for col in range(3):
        np.add.at(n, tris[:, col], fn)
    ln = np.linalg.norm(n, axis=1)
    ln[ln < 1e-15] = 1.0
    return n / ln[:, None]


def mesh_metrics(verts, faces):
    tris = _triangles(faces)
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    area = float(np.sum(np.linalg.norm(np.cross(b - a, c - a), axis=1)) * 0.5)
    volume = abs(_signed_volume(verts, faces))

    edges = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], axis=0)
    edges = np.sort(edges, axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = int(np.sum(counts == 1))
    nonmanifold = int(np.sum(counts > 2))
    euler = int(len(verts) - len(uniq) + len(tris))

    # connected components over the face graph (union-find on vertices)
    parent = np.arange(len(verts), dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for tri in tris:
        for u, v in ((tri[0], tri[1]), (tri[1], tri[2])):
            ru, rv = find(int(u)), find(int(v))
            if ru != rv:
                parent[ru] = rv
    roots = {find(i) for i in range(len(verts))}
    components = len(roots)

    lo = verts.min(axis=0)
    hi = verts.max(axis=0)
    return {
        "verts": int(len(verts)),
        "faces": int(len(faces)),
        "tris": int(len(tris)),
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "euler": euler,
        "components": int(components),
        "closed": bool(boundary == 0 and nonmanifold == 0),
        "volume_mm3": round(volume, 3),
        "area_mm2": round(area, 3),
        "bbox_mm": [[round(float(x), 3) for x in lo], [round(float(x), 3) for x in hi]],
        "size_mm": [round(float(x), 3) for x in (hi - lo)],
    }


def sample_surface(verts, normals, max_points, seed=20261031):
    n = len(verts)
    if n <= max_points:
        return verts.copy(), normals.copy(), np.arange(n)
    rng = np.random.default_rng(int(seed))
    sel = np.sort(rng.choice(n, size=int(max_points), replace=False))
    return verts[sel], normals[sel], sel


def band_samples(F, origin, spacing, max_points=520, seed=20261031):
    """Deterministic shell samples + field normals (no mesh dependency)."""
    band = np.abs(F) <= 1.25 * float(np.max(spacing))
    idx = np.argwhere(band)
    if idx.shape[0] == 0:
        raise ValueError("empty surface band")
    if idx.shape[0] > max_points:
        rng = np.random.default_rng(int(seed))
        sel = rng.choice(idx.shape[0], size=int(max_points), replace=False)
        idx = idx[sel]
    gx, gy, gz = np.gradient(F, *[float(s) for s in spacing], edge_order=1)
    n = np.stack([gx[idx[:, 0], idx[:, 1], idx[:, 2]],
                  gy[idx[:, 0], idx[:, 1], idx[:, 2]],
                  gz[idx[:, 0], idx[:, 1], idx[:, 2]]], axis=1)
    ln = np.linalg.norm(n, axis=1)
    ln[ln < 1e-15] = 1.0
    n = n / ln[:, None]
    P = origin[None, :] + idx.astype(np.float64) * spacing[None, :]
    return P, n


def ray_thickness(genome, P, N, max_t, spacing, steps=32):
    """2 * max |f| along the inward normal: a lower bound on local thickness.

    For a slab of thickness T this returns ~T; for a sphere it returns ~2R.
    Samples that never leave the shape are flagged `censored`.
    """
    # Shell samples may sit slightly outside the zero level; step them just
    # inside before marching so t=0 is never already an exit.
    f0 = build_field(genome, P)
    shift = np.where(f0 >= 0.0, f0 + 0.5 * float(np.max(spacing)), 0.0)
    P0 = P - N * shift[:, None]

    ts = np.linspace(0.0, float(max_t), int(steps))
    Q = P0[:, None, :] - N[:, None, :] * ts[None, :, None]
    Fq = build_field(genome, Q.reshape(-1, 3)).reshape(P.shape[0], ts.size)

    # Stop at the first exit (f >= 0).  Beyond the far surface |f| grows
    # again and would falsely look like a huge thickness.
    exits = Fq >= 0.0
    has_exit = exits.any(axis=1)
    first = np.argmax(exits, axis=1)
    first = np.where(has_exit, first, ts.size)
    rows = np.arange(Fq.shape[0])
    a = np.abs(Fq)
    a[np.arange(ts.size)[None, :] >= first[:, None]] = -1.0
    j = np.argmax(a, axis=1)
    coarse = np.maximum(a[rows, j], 0.0)

    # local refinement around the coarse maximum (the field is cheap to sample)
    lo = ts[np.maximum(j - 1, 0)]
    hi = ts[np.minimum(j + 1, ts.size - 1)]
    fine_t = lo[:, None] + (hi - lo)[:, None] * np.linspace(0.0, 1.0, 17)[None, :]
    Q2 = P0[:, None, :] - N[:, None, :] * fine_t[:, :, None]
    F2 = build_field(genome, Q2.reshape(-1, 3)).reshape(P.shape[0], fine_t.shape[1])
    fine = np.max(np.abs(F2), axis=1)
    best = np.maximum(coarse, fine)

    th = 2.0 * best
    censored = ~has_exit
    return th, censored
