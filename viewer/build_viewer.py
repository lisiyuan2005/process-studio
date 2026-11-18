"""把一个运行目录打包成可发布的网页：index.html + GLB + 截面 PNG。

用法：python viewer/build_viewer.py runs/<run> site/<name>
"""
import base64
import json
import shutil
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent


def main(run_dir, out_dir):
    run, out = Path(run_dir), Path(out_dir)
    (out / "glb").mkdir(parents=True, exist_ok=True)
    (out / "sec").mkdir(parents=True, exist_ok=True)
    perf = json.loads((run / "perf.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    recipe = yaml.safe_load((run / "recipe.yaml").read_text())
    params = {s["id"]: {k: v for k, v in s.items() if k not in ("id", "op")} for s in recipe["steps"]}
    params["s01_substrate"] = recipe["initial"]["substrate"]
    steps = []
    for s in perf["steps"]:
        n = s["index"] + 1 if s["index"] == 0 else s["index"]
        name = f"{n:02d}_{s['id']}"
        glb, png = run / "exports" / f"{name}.glb", run / "exports" / f"{name}.png"
        # Artifact 托管不接受 .glb，且页面 CSP 不允许 fetch data: URI：
        # GLB 原样 base64 存为 .txt，由页面解码后交给 GLTFLoader.parse()，不再发起任何请求
        model = out / "glb" / f"{name}.glb.b64.txt"
        if glb.exists():
            model.write_text(base64.b64encode(glb.read_bytes()).decode())
        if png.exists():
            shutil.copy(png, out / "sec" / png.name)
        steps.append({
            "n": n, "id": s["id"], "op": s["op"], "params": params.get(s["id"], {}),
            "glb": f"glb/{model.name}" if glb.exists() else None,
            "png": f"sec/{png.name}" if png.exists() else None,
            "compute_s": s["compute"]["wall_s"], "cpu_s": s["compute"]["cpu_s"],
            "voxel_s": s.get("voxelize", {}).get("wall_s"),
            "export_s": s.get("export", {}).get("wall_s"),
            "peak_mb": s["compute"].get("phase_peak_mb", s.get("export", {}).get("peak_rss_mb")),
            "n_ls": s.get("n_levelsets"), "n_pts": s.get("n_points"),
            "volumes": s.get("volumes_nm3", {}), "faces": s.get("glb_faces", {}),
            "error": s.get("error"),
        })
    data = {
        "recipe": recipe["name"], "grid": perf["grid_nm"], "host": perf["host"],
        "backend": manifest["backend"], "backend_version": manifest.get("backend_version"),
        "materials": recipe["materials"], "domain": recipe["domain"],
        "startup_s": perf.get("startup_imports", {}).get("wall_s"),
        "total_s": perf.get("total_wall_s"), "steps": steps,
    }
    html = (HERE / "template.html").read_text()
    html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(html)
    print(out / "index.html")


if __name__ == "__main__":
    main(*sys.argv[1:3])
