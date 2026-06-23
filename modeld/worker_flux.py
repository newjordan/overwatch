#!/usr/bin/env python3
"""FLUX.2 [klein] 4B text->image worker for modeld. Runs inside the `flux2` conda env.
Loads black-forest-labs/FLUX.2-klein once, then per request {params:{prompt,steps,job_id,out_dir}}
generates a PNG, replying {ok,output,mime}.

NOTE: FLUX.2-klein released Jan 2026 (after this tool's author's training cutoff). The exact
diffusers loader class is taken from its model card at deploy time; the call below is the
expected shape and may need the precise class name pinned during env setup.
"""
import sys, json, os

def emit(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()

try:
    import torch
    try:
        from diffusers import Flux2Pipeline as _Pipe          # expected FLUX.2 class
    except Exception:
        from diffusers import DiffusionPipeline as _Pipe       # fallback: generic loader
    PIPE = _Pipe.from_pretrained("black-forest-labs/FLUX.2-klein", torch_dtype=torch.bfloat16)
    PIPE = PIPE.to("cuda")
except Exception as e:
    emit({"error": f"{type(e).__name__}: {e}"}); sys.exit(1)

emit({"ready": True})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        p = (json.loads(line).get("params") or {})
        out = os.path.join(p.get("out_dir", "."), p.get("job_id", "flux") + ".png")
        image = PIPE(prompt=p["prompt"],
                     num_inference_steps=int(p.get("steps", 4)),
                     guidance_scale=float(p.get("guidance", 3.5))).images[0]
        image.save(out)
        emit({"ok": True, "output": out, "mime": "image/png"})
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
