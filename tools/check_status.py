# -*- coding: utf-8 -*-
import urllib.request, json, sys
sys.stdout.reconfigure(encoding="utf-8")

r = urllib.request.urlopen("http://127.0.0.1:7860/api/proto/status", timeout=5)
data = json.loads(r.read())
print(f"running={data.get('running')} task_count={len(data.get('tasks', {}))}")
print("-" * 60)
for k, t in data["tasks"].items():
    success = t["success"]
    elapsed = t.get("elapsed", 0) or 0
    print(f"[{k}] success={success} elapsed={elapsed:.1f}s email={t['email']}")
    print(f"    step:  {t['step']}")
    print(f"    error: {t['error']}")
    print()
