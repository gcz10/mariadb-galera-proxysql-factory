#!/usr/bin/env python3
"""
Warstwa LLM-as-judge — ocena MERYTORYCZNA odpowiedzi wyszukiwarek.

Roznica wobec run.py: fact_hits szuka slow kluczowych (prosty grep), ten skrypt
kazde modelowi orzec, czy odpowiedz jest poprawna. Werdykt 3-stanowy:

  CORRECT    - odpowiedz merytorycznie poprawna (lub zrodla wystarczaja, by ja z nich wyczytac)
  INCORRECT  - odpowiedz merytorycznie bledna
  UNSUPPORTED- za malo materialu (brak prozy u indeksow, za krotkie snippety)

Uzycie:
  python3 judge.py             # ocen wszystkie nieocenione rekordy
  python3 judge.py --provider brave   # tylko jednego dostawce
  python3 judge.py --rejudge   # ocen ponownie wszystkie (nadpisuje)
"""
import json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import run  # fetch(), QUESTIONS, PROVIDERS

HISTORY = ROOT/'results/history.jsonl'
JUDGED = ROOT/'results/judged.jsonl'

# Pytania o rozstrzygalnej poprawnosci — systemd-oom celowo pominiety:
# odpowiedz jest oficjalnie nieudokumentowana (systemd/systemd#39187),
# sedzia nie ma czym rozstrzygnac.
JUDGEABLE = {'galera-flow','k8s-terminating','dns-ttl','mercury','eu-vat'}

PROMPT_TEMPLATE = """Jestes sedzia w teście porownawczym wyszukiwarek. Oceniasz, czy odpowiedz na pytanie jest merytorycznie poprawna.

Pytanie: {pytanie}

Fakty oczekiwane (poprawna odpowiedz musi je zawierac lub z nich wynikac):
{fakty}

Oczekiwana poprawna odpowiedz (wzorzec do porownania, nie jedyna dopuszczalna forma):
{oczekiwana}

Odpowiedz dostawcy (moze byc pusta dla czystych indeksow):
---ODPOWIEDZ---
{odpowiedz}
---KONIEC---

Pobrane zrodla (tekst stron wskazanych przez dostawce, przyciety):
---ZRODLA---
{zrodla}
---KONIEC---

Zasady:
1. Poprawnosc orzekaj wobec stanu wiedzy na 2026-08-05.
2. Porownaj odpowiedz dostawcy z Oczekiwana poprawna odpowiedz. Odpowiedz moze byc
   inaczej sformulowana, byle merytorycznie zgodna. NIE wymagaj dokladnego powtorzenia.
3. PUSTA odpowiedz NIE jest bledem. Dla pustej odpowiedzi oceń, czy ZRODLA wystarczaja,
   by wyczytac z nich poprawna odpowiedz:
   - zrodla zawieraja wymagane fakty -> CORRECT
   - zrodla niekompletne -> UNSUPPORTED
4. INCORRECT TYLKO, gdy odpowiedz zawiera jednoznaczny blad merytoryczny
   (np. bledna wartosc, bledny mechanizm). Brak odpowiedzi nigdy nie jest INCORRECT.
5. Watpliwosci -> UNSUPPORTED. Zrodla puste/za krotkie -> UNSUPPORTED.

Odpowiedz WYLACZNIE JSON-em:
{{"werdykt": "CORRECT|INCORRECT|UNSUPPORTED", "uzasadnienie": "1-2 zdania po polsku"}}"""

def fact_context(texts, facts, width=400):
    """Wytnij fragmenty wokol kluczowych faktow zamiast slepego ciecia."""
    parts = []
    for t in texts:
        if not t:
            continue
        t = re.sub(r'\s+', ' ', t)
        for f in facts:
            i = t.lower().find(f.lower())
            if i >= 0:
                parts.append(f'[{f}]: ' + t[max(0,i-width):i+width])
        if len(parts) >= 8:
            break
    return '\n\n'.join(parts) or '(brak kontekstu w zrodlach)'

def judge_question(q, provider_rec):
    """Zapytaj model sedziego. Zwraca (werdykt, uzasadnienie)."""
    texts = [run.fetch(u) for u in provider_rec.get('urls', [])[:3]]
    zrodla = fact_context(texts, q['kluczowe_fakty'])
    prompt = PROMPT_TEMPLATE.format(
        pytanie=q['pytanie'],
        fakty=', '.join(q['kluczowe_fakty']),
        oczekiwana=q.get('oczekiwana_odpowiedz','(brak wzorca — oceń samodzielnie)'),
        odpowiedz=(provider_rec.get('answer') or '(brak)')[:2000],
        zrodla=zrodla[:6000],
    )
    tok = subprocess.run(['omp','token','ollama-cloud'], capture_output=True, text=True).stdout.strip().split('\n')[0]
    body = json.dumps({'model':'deepseek-v4-flash','stream':False,
                       'messages':[{'role':'user','content':prompt}]})
    r = subprocess.run(['curl','-s','--max-time','90','https://ollama.com/api/chat',
                        '-H',f'Authorization: Bearer {tok}','-H','Content-Type: application/json',
                        '-d',body], capture_output=True, text=True, timeout=120)
    try:
        txt = json.loads(r.stdout)['message']['content']
    except Exception:
        return 'UNSUPPORTED', f'blad sedziego: {r.stdout[:100]}'
    import re
    m = re.search(r'\{.*\}', txt, re.DOTALL)
    if not m:
        return 'UNSUPPORTED', 'sedzia nie zwrocil JSON'
    try:
        d = json.loads(m.group(0))
        return d.get('werdykt','UNSUPPORTED'), d.get('uzasadnienie','')
    except Exception:
        return 'UNSUPPORTED', 'zly JSON sedziego'

def main():
    args = sys.argv[1:]
    only = args[args.index('--provider')+1] if '--provider' in args else None
    rejudge = '--rejudge' in args

    rows = [json.loads(l) for l in HISTORY.read_text().strip().split('\n') if l]
    done = {}
    if JUDGED.exists() and not rejudge:
        for l in JUDGED.read_text().strip().split('\n'):
            if l:
                j = json.loads(l)
                done[(j['provider'], j['question_id'], j['proba'])] = j

    qmap = {q['id']: q for q in run.QUESTIONS}
    new = 0
    with JUDGED.open('a') as out:
        for r in rows:
            if r.get('error') or r.get('timeout'):
                continue
            if r['question_id'] not in JUDGEABLE:
                continue
            if only and r['provider'] != only:
                continue
            key = (r['provider'], r['question_id'], r['proba'])
            if key in done:
                continue
            q = qmap[r['question_id']]
            verdict, why = judge_question(q, r)
            rec = {'provider':r['provider'],'question_id':r['question_id'],'proba':r['proba'],
                   'werdykt':verdict,'uzasadnienie':why,
                   'ts':__import__('time').strftime('%Y-%m-%d %H:%M')}
            out.write(json.dumps(rec, ensure_ascii=False)+'\n')
            out.flush()
            new += 1
            print(f"[{new}] {rec['provider']:10s} {rec['question_id']:16s} {verdict:10s} {why[:60]}")
    print(f"\noceniono {new} nowych rekordow -> {JUDGED}")

if __name__ == '__main__':
    main()
