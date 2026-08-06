#!/usr/bin/env python3
"""
Websearch benchmark — powtarzalny, inkrementalny, z ocena trafnosci.

Uzycie:
  python3 run.py                    # pelny przebieg (6 dostawcow x 6 pytan x 3 proby)
  python3 run.py --provider brave   # tylko jeden dostawca
  python3 run.py --repeat 1         # jedna proba zamiast trzech
  python3 run.py --dry-run          # pokaz plan, nie uruchamiaj
"""
import json, re, subprocess, sys, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
QUESTIONS = json.loads((ROOT/'questions.json').read_text())
HISTORY = ROOT/'results/history.jsonl'
HISTORY.parent.mkdir(exist_ok=True)

PROVIDERS = ['synthetic','brave','exa','anthropic','gemini']
# codex, zai, public celowo wylaczone — udowodnione niesprawne

PRIMARY = {'mariadb.com','percona.com','cdn.kernel.org','freedesktop.org','github.com',
           'kubernetes.io','lkml.iu.edu','docs.redhat.com','access.redhat.com','gitlab.com',
           'postgresql.org','discuss.kubernetes.io',
           # nowe z benchmarku
           'ciaaw.org','periodic-table.rsc.org','pubchem.ncbi.nlm.nih.gov','iupac.org',
           'wikipedia.org','proquest.com'}
QA = {'stackoverflow.com','serverfault.com','unix.stackexchange.com','reddit.com'}
# reszta = nieznane, nie 'szum'

def heal_url(u):
    """Napraw URL zepsuty przez renderer terminala.

    omp zawija dlugie URL-e i doklej ogon, albo drukuje link dwa razy
    (tekst + cel hiperlacza). Wspolna cecha: doklejony ogon jest sufiksem
    tego, co juz jest w napisie. Przyklady:
      .../flow-control-in-galera-cluster + era-cluster
      .../llms.txt + llms.txt
    """
    u = u.rstrip('.,;:)]}>("\'')
    for _ in range(3):  # zawijanie moze zajsc kilka razy
        zmiana = False
        for k in range(min(len(u)//2, 80), 3, -1):
            if u[:-k].endswith(u[-k:]):
                u = u[:-k]
                zmiana = True
                break
        if not zmiana:
            break
    return u

def unicode_digits_to_ascii(s):
    """Gemini uzywa cyfr matematycznych Unicode (U+1D7EC...). Normalizuj do ASCII."""
    for code in range(0x1D7CE, 0x1D800):  # math digits 0-9 x4 style
        s = s.replace(chr(code), chr(0x30 + (code - 0x1D7CE) % 10))
    return s

def run_provider(prov, question, limit=8):
    # bez --compact: renderer condensed obcina dlugie URL-e wielokropkiem
    cmd = ['omp','search','--provider',prov,'-l',str(limit),question]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        sec = round(time.time()-t0,1)
    except subprocess.TimeoutExpired:
        return {'error':'timeout'}
    txt = unicode_digits_to_ascii(re.sub(r'\x1b\[[0-9;]*m|\x07', '', r.stdout))
    err_m = re.search(r'✘.*?(?:\n│\s*Error:)?\s*(?:Error:\s*)?(.{5,120})', txt)
    err = None
    if 'Error:' in txt or '✘' in txt:
        m = re.search(r'Error:\s*(.{5,150})', txt)
        err = (m.group(1).strip() if m else 'provider error')
    if err:
        return {'error': err[:150], 'sec': sec}
    # URL-e moga byc owijane lub podwojone (tekst + cel hiperlacza w jednej linii)
    srcs = []
    for m in re.finditer(r'https?://[^\s\x1b\x07\)\]"\'|>]+', txt):
        u = m.group(0)
        # obetnij przy drugim 'https://' — to cel hiperlacza
        idx = u.find('https://', 8)
        if idx > 0:
            u = u[:idx]
        if '\u2026' in u:   # renderer skrocil URL wielokropkiem — nieuzywalny
            continue
        srcs.append(heal_url(u))
    srcs = list(dict.fromkeys(srcs))  # deduplikacja, zachowuje kolejnosc
    doms = [urlparse(u).netloc.replace('www.','') for u in srcs]
    m = re.search(r'Provider:\s*(\S+)', txt)
    # ekstrakcja odpowiedzi liniowa — odporna na znaki ramki box-drawing
    lines = txt.split('\n')
    ans_lines = []
    in_ans = False
    for ln in lines:
        if 'Answer' in ln:
            in_ans = True
            continue
        if in_ans:
            if 'Sources' in ln:
                break
            ans_lines.append(ln)
    answer = re.sub(r'\s+',' ', ' '.join(ans_lines)).strip()[:2000]
    answer = re.sub(r'[│╭╰├─┤]', '', answer)
    answer = re.sub(r'\s+', ' ', answer).strip()
    # gemini podaje zrodla jako gole domeny: "ciaaw.org (vertexaisearch...)"
    dom_m = re.findall(r'├─\s*([a-z0-9.-]+\.[a-z]{2,})\s*\(', txt)
    for d in dom_m:
        if d not in doms:
            doms.append(d)
    return {'sec':sec, 'n_src':len(srcs), 'urls':srcs[:5], 'doms':doms, 'answer':answer,
            'raw':txt[:8000], 'engine':m.group(1) if m else prov,
            'error':None if r.returncode==0 else f'rc={r.returncode}'}

def fetch(url, timeout=15):
    """Pobierz strone przez curl — urllib dostaje blokady/403 z Cloudflare."""
    try:
        r = subprocess.run(['curl','-sL','--max-time',str(timeout),
                            '-A','Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36',
                            url], capture_output=True, text=True, timeout=timeout+5)
        return re.sub(r'<[^>]+>',' ', r.stdout)
    except Exception:
        return ''

def score_hit(question, doms, fetched_texts, answer_text=''):
    """Ocena trafnosci: fakty w pobranych zrodlach LUB w samej odpowiedzi."""
    facts = question['kluczowe_fakty']
    combined = (' '.join(fetched_texts) + ' ' + answer_text).lower()
    hits = [f for f in facts if f.lower() in combined]
    prim = sum(1 for d in doms if d in PRIMARY)
    qa   = sum(1 for d in doms if d in QA)
    unknown = len(doms) - prim - qa
    return {'fact_hits': f"{len(hits)}/{len(facts)}", 'facts_found': hits,
            'prim': prim, 'qa': qa, 'unknown': unknown}

def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    only = args[args.index('--provider')+1] if '--provider' in args else None
    repeat = int(args[args.index('--repeat')+1]) if '--repeat' in args else 3

    plan = [(p,q['id'],i) for p in ([only] if only else PROVIDERS)
                          for q in QUESTIONS for i in range(repeat)]
    if dry:
        print(f"plan: {len(plan)} wywolan")
        for p,q,i in plan[:6]: print(f"  {p:10s} {q:18s} proba {i+1}")
        print(f"  ... i {len(plan)-6} wiecej")
        return

    done = set()
    if HISTORY.exists():
        for line in HISTORY.read_text().strip().split('\n'):
            if line:
                r = json.loads(line)
                done.add((r['provider'], r['question_id'], r['proba']))

    qmap = {q['id']: q for q in QUESTIONS}
    new = 0
    with HISTORY.open('a') as out:
        for prov, qid, proba in plan:
            if (prov, qid, proba) in done:
                continue
            q = qmap[qid]
            res = run_provider(prov, q['pytanie'])
            if res.get('error'):
                rec = {'provider':prov,'question_id':qid,'proba':proba,
                       'ts':time.strftime('%Y-%m-%d %H:%M'),
                       'error':res.get('error'), 'timeout':res.get('error')=='timeout'}
            else:
                texts = []
                for u in res['urls'][:3]:
                    texts.append(fetch(u))
                rec = {'provider':prov,'question_id':qid,'proba':proba,
                       'ts':time.strftime('%Y-%m-%d %H:%M'),
                       **{k:v for k,v in res.items() if k not in ('error','raw')},
                       **score_hit(q, res['doms'], texts, res.get('answer','') + '\n' + res.get('raw',''))}
            out.write(json.dumps(rec, ensure_ascii=False)+'\n')
            out.flush()
            new += 1
            print(f"[{new}] {prov:10s} {qid:18s} p{proba+1} "
                  f"{rec.get('sec','?'):>5}s fakty={rec.get('fact_hits','') or rec.get('error','')[:40]}")
            time.sleep(2)

    print(f"\nzapisano {new} nowych wynikow -> {HISTORY}")
    print("raport: python3 report.py")
    # podsumowanie bledow
    errs = [json.loads(l) for l in HISTORY.read_text().strip().split('\n') if l and 'error' in json.loads(l)]
    if errs:
        print(f"\nUWAGA: {len(errs)} wywolan z bledem dostawcy:")
        for e in errs[:5]:
            print(f"  {e['provider']:10s} {e['question_id']:16s} {e.get('error','')[:70]}")

if __name__ == '__main__':
    main()
