"""列出 ViennaPS / ViennaLS 的 Python API，供编写后端时查阅。"""
import inspect, viennaps, viennals
def dump(mod, name, depth=0):
    for k in sorted(dir(mod)):
        if k.startswith("_"): continue
        obj = getattr(mod, k)
        if inspect.ismodule(obj) and depth < 1 and obj.__name__.startswith(("viennaps", "viennals")):
            print(f"\n## module {name}.{k}")
            dump(obj, f"{name}.{k}", depth + 1)
            continue
        doc = (getattr(obj, "__doc__", "") or "").strip().splitlines()
        print(f"{name}.{k}: {doc[0][:200] if doc else ''}")
        if inspect.isclass(obj):
            for m in sorted(dir(obj)):
                if m.startswith("_") and m != "__init__": continue
                d = (getattr(getattr(obj, m), "__doc__", "") or "").strip().splitlines()
                print(f"    .{m}: {' | '.join(d[:4])[:400]}")
for mod, name in [(viennaps, "viennaps"), (viennals, "viennals")]:
    print(f"# {name}")
    dump(mod, name)
