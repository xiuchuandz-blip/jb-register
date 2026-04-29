# -*- coding: utf-8 -*-
import json, sys
sys.stdout.reconfigure(encoding="utf-8")

data = json.loads(open(r"D:\GhostRegV2\output\protocol_accounts.json", encoding="utf-8").read())
print(f"账号总数: {len(data)}")
print(f"最新 5 个:")
for a in data[-5:]:
    aid = a.get("id") or "-"
    print(f"  id={aid!s:>3} {a.get('email'):<40} pwd={a.get('password')} time={a.get('time','-')}")
