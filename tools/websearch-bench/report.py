#!/usr/bin/env python3
"""Raport z historii benchmarku — trend w czasie."""
import json
from collections import defaultdict
from pathlib import Path

H = Path(__file__).parent/'results/history.jsonl'
rows = [json.loads(l) for l in H.read_text().strip().split('\n') if l]

# grupuj po (dostawca, data)
by = defaultdict(list)
for r in rows:
    by[(r['provider'], r['ts'][:10])].append(r)

print(f"{'dostawca':11s} {'data':12s} {'n':>3s} {'fakty %':>8s} {'pierwotne':>10s} {'czas s':>7s}")
print('-'*56)
for (prov, day), rs in sorted(by.items()):
    ok = [r for r in rs if not r.get('timeout') and not r.get('error')]
    if not ok:
        errs = [r.get('error','?')[:30] for r in rs if r.get('error')]
        print(f"{prov:11s} {day:12s} {len(rs):>3d} {'ERROR':>8s} {('; '.join(errs[:2]))[:20]}")
        continue
    fh = [tuple(map(int, r['fact_hits'].split('/'))) for r in ok if '/' in r.get('fact_hits','')]
    fpct = round(100*sum(a for a,b in fh)/sum(b for a,b in fh)) if fh else 0
    prim = round(100*sum(r['prim'] for r in ok)/sum(r['n_src'] or 1 for r in ok))
    sec  = round(sum(r.get('sec',0) for r in ok)/len(ok),1)
    print(f"{prov:11s} {day:12s} {len(ok):>3d} {fpct:>6d}% {prim:>8d}% {sec:>6g}s")
