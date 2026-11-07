# -*- coding: utf-8 -*-
"""Blender-side runner: evaluate a job, build/render meshes, export GLB.

Invoked as:
    blender --background --factory-startup --python blender_runner.py -- job.json
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

import pipeline  # noqa: E402


AMBER = (0.82, 0.38, 0.12, 1.0)
GROUND = (0.10, 0.105, 0.115, 1.0)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 24
    sc.cycles.use_denoising = True
    try:
        sc.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    sc.cycles.max_bounces = 4
    sc.cycles.diffuse_bounces = 2
    sc.cycles.glossy_bounces = 2
    sc.cycles.transparent_max_bounces = 2
    sc.render.resolution_x = 920
    sc.render.resolution_y = 720
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = False
    sc.render.image_settings.color_mode = "RGB"
    sc.view_settings.view_transform = "AgX"
    try:
        sc.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        try:
            sc.view_settings.look = "None"
        except Exception:
            pass
    sc.view_settings.exposure = 0.0
    world = bpy.data.worlds.new("SoftWorld")
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs[0].default_value = (0.035, 0.04, 0.05, 1.0)
        bg.inputs[1].default_value = 0.10
    return sc


def make_material():
    mat = bpy.data.materials.get("SoftAmber") or bpy.data.materials.new("SoftAmber")
    mat.use_nodes = True
    bsdf = None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            bsdf = n
            break
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.78, 0.30, 0.075, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.34
        bsdf.inputs["Metallic"].default_value = 0.0
    mat.diffuse_color = AMBER
    return mat


def add_ground(z, size):
    me = bpy.data.meshes.new("Ground")
    s = size * 4.0
    me.from_pydata([(-s, -s, z), (s, -s, z), (s, s, z), (-s, s, z)], [], [(0, 1, 2, 3)])
    me.update()
    ob = bpy.data.objects.new("Ground", me)
    bpy.context.collection.objects.link(ob)
    mat = bpy.data.materials.get("SoftGround") or bpy.data.materials.new("SoftGround")
    mat.use_nodes = True
    b = None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            b = n
            break
    if b is not None:
        b.inputs["Base Color"].default_value = (0.055, 0.06, 0.07, 1.0)
        b.inputs["Roughness"].default_value = 0.55
    mat.diffuse_color = GROUND
    ob.data.materials.append(mat)
    return ob


def add_lights(center, size):
    specs = [
        ("Key", (1.5, -1.8, 1.7), 3.2, 0.10, (1.0, 0.93, 0.85)),
        ("Fill", (-1.8, -1.0, 0.7), 0.9, 0.30, (0.75, 0.82, 1.0)),
        ("Rim", (-0.6, 1.9, 1.4), 2.0, 0.20, (1.0, 0.74, 0.48)),
    ]
    for name, d, energy, angle, color in specs:
        data = bpy.data.lights.new(name, "SUN")
        data.energy = energy
        data.angle = angle
        data.color = color
        ob = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(ob)
        direction = Vector(d).normalized()
        ob.location = Vector(center) + direction * max(size, 0.01) * 2.0
        ob.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()


def add_mesh(verts, faces, name="SoftShape"):
    me = bpy.data.meshes.new(name)
    me.from_pydata(np.asarray(verts).tolist(), [], np.asarray(faces).tolist())
    fixed = me.validate(clean_customdata=False)
    me.update()
    for poly in me.polygons:
        poly.use_smooth = True
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(make_material())
    return ob, fixed


def frame_camera(sc, verts):
    v = np.asarray(verts)
    lo, hi = v.min(axis=0), v.max(axis=0)
    center = (lo + hi) * 0.5
    size = float(np.max(hi - lo)) or 1.0
    cam_data = bpy.data.cameras.new("SoftCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = size * 1.32
    cam = bpy.data.objects.new("SoftCam", cam_data)
    bpy.context.collection.objects.link(cam)
    d = Vector((1.15, -1.65, 1.05)).normalized()
    cam.location = Vector(center) + d * (size * 3.0 + 60.0)
    look = Vector(center) - cam.location
    cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    sc.camera = cam
    return cam, (lo, hi)


def render_snapshot(snap, out_png):
    sc = reset()
    ob, fixed = add_mesh(snap["verts"], snap["faces"], "Shape_" + str(snap.get("label", "x")))
    v = np.asarray(snap["verts"])
    lo, hi = v.min(axis=0), v.max(axis=0)
    center = (lo + hi) * 0.5
    size = float(np.max(hi - lo)) or 1.0
    add_ground(float(lo[2]) - size * 0.012, size)
    add_lights(Vector(center), size)
    frame_camera(sc, snap["verts"])
    sc.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    return {"fixed_by_validate": bool(fixed), "verts": len(snap["verts"]), "faces": len(snap["faces"])}


def export_glb(snap, path):
    reset()
    ob, _ = add_mesh(snap["verts"], snap["faces"], "SoftFinal")
    bpy.ops.object.select_all(action="DESELECT")
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)
    return os.path.getsize(path)


def main():
    argv = sys.argv
    if "--" not in argv or len(argv) < argv.index("--") + 2:
        raise SystemExit("usage: blender_runner.py -- job.json")
    job_path = argv[argv.index("--") + 1]
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)
    out_dir = job.get("out_dir") or os.path.dirname(os.path.abspath(job_path))
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    payload = pipeline.run_job(job)
    result = payload["result"]
    snaps = payload.get("snapshots") or []
    if not snaps:
        raise RuntimeError("pipeline returned no snapshots")

    previews = []
    for i, snap in enumerate(snaps):
        label = str(snap.get("label", "snap%d" % i))
        png = os.path.join(out_dir, "preview_%02d_%s.png" % (i, label))
        meta = render_snapshot(snap, png)
        previews.append({"label": label, "t": snap.get("t"), "path": png,
                         "geometry": snap.get("geometry"), "render": meta})
        print("SOFT_RENDER %s" % png, flush=True)

    final = snaps[-1]
    glb = os.path.join(out_dir, "shape.glb")
    try:
        glb_bytes = export_glb(final, glb)
        glb_path = glb
    except Exception as e:
        glb_path = None
        glb_bytes = None
        result.setdefault("notes", []).append("GLB export failed: %s: %s" % (type(e).__name__, str(e)[:200]))

    # attach the last full energy report (the snapshots already carry geometry)
    result["artifacts"] = dict(result.get("artifacts") or {})
    result["artifacts"].update({
        "previews": previews,
        "hero": previews[-1]["path"],
        "glb": glb_path,
        "glb_bytes": glb_bytes,
        "out_dir": out_dir,
    })
    result["blender"] = bpy.app.version_string
    result["elapsed_s"] = round(time.time() - t0, 2)
    result["snapshots"] = [{"label": p["label"], "t": p["t"], "geometry": p["geometry"]} for p in previews]

    out_json = os.path.join(out_dir, "result.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("SOFT_RESULT %s" % out_json, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = {"ok": False, "error": traceback.format_exc()[-4000:]}
        argv = sys.argv
        job_path = argv[argv.index("--") + 1] if "--" in argv and len(argv) > argv.index("--") + 1 else None
        if job_path:
            try:
                with open(job_path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                od = j.get("out_dir") or os.path.dirname(os.path.abspath(job_path))
                os.makedirs(od, exist_ok=True)
                with open(os.path.join(od, "error.json"), "w", encoding="utf-8") as f:
                    json.dump(err, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        print("SOFT_ERROR %s" % err["error"].replace("\n", " | ")[-1500:], flush=True)
        raise
