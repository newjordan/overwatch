#!/usr/bin/env python3
"""TRELLIS.2 image->3D worker for modeld. Runs inside the `trellis2` conda env.
Loads microsoft/TRELLIS.2-4B once, then per JSONL request {params:{image,job_id,out_dir}}
runs the pipeline and exports a .glb, replying {ok,output,mime}.
"""
import sys, json, os
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

def emit(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()

try:
    import torch  # noqa
    from PIL import Image
    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    import o_voxel
    PIPE = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
    PIPE.cuda()
except Exception as e:  # surfaced by modeld as 503 (e.g. broken env / missing torch)
    emit({"error": f"{type(e).__name__}: {e}"}); sys.exit(1)

emit({"ready": True})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        p = (json.loads(line).get("params") or {})
        img = Image.open(p["image"]).convert("RGB")
        mesh = PIPE.run(img)[0]
        mesh.simplify(16777216)
        out = os.path.join(p.get("out_dir", "."), p.get("job_id", "trellis") + ".glb")
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs,
            coords=mesh.coords, attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]], decimation_target=1000000,
            texture_size=int(p.get("texture_size", 2048)), remesh=True, remesh_band=1,
            remesh_project=0, verbose=False)
        glb.export(out, extension_webp=True)
        emit({"ok": True, "output": out, "mime": "model/gltf-binary"})
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
