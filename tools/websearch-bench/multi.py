#!/usr/bin/env python3
"""
Rownolegle wyszukiwanie w kilku silnikach naraz + scalanie po zgodzie.

omp tego nie potrafi: `providers.webSearchOrder` to kolejka AWARYJNA — zwraca
wynik pierwszego dostawcy, ktory cokolwiek odda ("There is no provider-level
parallel fan-out; fallback is sequential"). Jedyny agregator wbudowany, `public`,
obejmuje wylacznie darmowe skrobaczki (startpage/google/duckduckgo/ecosia/mojeek)
i wymaga headless Chromium.

Ten skrypt robi to samo co `public`, ale nad dostawcami, ktore realnie dzialaja.
Algorytm scalania skopiowany z public.ts:
  - dedup po kluczu kanonicznym (host bez www, bez fragmentu, znormalizowany /)
  - ranking po ZGODZIE miedzysilnikowej (ile silnikow zwrocilo ten sam URL)
  - remis rozstrzyga najlepsza pozycja w pojedynczym silniku
  - wygrywa najdluzszy tytul/snippet

Uzycie:
  python3 multi.py "galera gcache sizing"
  python3 multi.py "pytanie" --providers synthetic,brave,exa,gemini
  python3 multi.py "pytanie" --limit 15 --json
"""
import concurrent.futures as cf
import json, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).parent))
import run  # run_provider() — parser odporny na ANSI/BEL/sklejone URL-e

DEFAULT_PROVIDERS = ['synthetic', 'brave', 'exa']

def canonical(url):
    """Klucz kanoniczny jak w public.ts: host bez www, bez fragmentu, znorm. ukosnik."""
    try:
        p = urlparse(url)
    except Exception:
        return url
    host = p.netloc.lower().removeprefix('www.')
    path = p.path.rstrip('/') or '/'
    return urlunparse(('https', host, path, '', p.query, ''))

def search_one(prov, query, limit):
    t0 = time.time()
    try:
        res = run.run_provider(prov, query, limit=limit)
    except Exception as e:
        return prov, {'error': str(e)[:120], 'sec': round(time.time()-t0, 1)}
    return prov, res

def merge(per_provider):
    """Scal wyniki: zgoda miedzysilnikowa -> najlepsza pozycja -> najdluzszy tytul."""
    merged = {}
    for prov, res in per_provider.items():
        if res.get('error'):
            continue
        for rank, url in enumerate(res.get('urls', [])):
            key = canonical(url)
            e = merged.setdefault(key, {'url': url, 'engines': [], 'best_rank': 999, 'title': ''})
            if prov not in e['engines']:
                e['engines'].append(prov)
            e['best_rank'] = min(e['best_rank'], rank)
    out = list(merged.values())
    out.sort(key=lambda e: (-len(e['engines']), e['best_rank']))
    return out

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__.strip()); sys.exit(1)
    query = args[0]
    flags = sys.argv[1:]
    provs = DEFAULT_PROVIDERS
    if '--providers' in flags:
        provs = flags[flags.index('--providers')+1].split(',')
    limit = int(flags[flags.index('--limit')+1]) if '--limit' in flags else 10
    as_json = '--json' in flags

    t0 = time.time()
    per = {}
    with cf.ThreadPoolExecutor(max_workers=len(provs)) as ex:
        futs = [ex.submit(search_one, p, query, limit) for p in provs]
        for f in cf.as_completed(futs):
            prov, res = f.result()
            per[prov] = res
    total = round(time.time()-t0, 1)

    items = merge(per)
    if as_json:
        print(json.dumps({'query': query, 'sec': total, 'results': items}, ensure_ascii=False, indent=2))
        return

    print(f'"{query}"  —  {len(provs)} silnikow rownolegle, {total}s\n')
    for p in provs:
        r = per.get(p, {})
        status = r.get('error') or f"{r.get('n_src',0)} zrodel w {r.get('sec','?')}s"
        print(f"  {p:10s} {status}")
    print(f"\nPo scaleniu: {len(items)} unikalnych URL-i\n")
    for i, e in enumerate(items[:limit], 1):
        zgoda = '★' * len(e['engines'])
        print(f"{i:2d}. {zgoda:<4s} {e['url'][:95]}")
        print(f"      [{', '.join(e['engines'])}]")

if __name__ == '__main__':
    main()
