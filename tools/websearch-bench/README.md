# websearch-bench

Powtarzalny benchmark dostawcow wyszukiwania omp — z ocena trafnosci i wariancji.

## Uzycie

```bash
python3 run.py              # pelny przebieg: 6 dostawcow x 6 pytan x 3 proby
python3 run.py --provider brave   # tylko jeden dostawca
python3 run.py --repeat 1         # jedna proba (szybki test)
python3 run.py --dry-run          # pokaz plan, nie uruchamiaj
python3 report.py           # raport agregujacy z history.jsonl
```

Wyniki dopisuja sie do `results/history.jsonl` — przebieg jest **inkrementalny**,
ponownie uruchomiony nie powtarza wykonanych prob. Trend w czasie: raport grupuje
po (dostawca, data).

## Pytania

`questions.json` — kazde pytanie ma `kluczowe_fakty`: poprawne odpowiedzi, ktorych
szukamy w pobranych zrodlach. Szesc pytan o roznym charakterze:

| id | typ | jezyk |
|---|---|---|
| galera-flow | konkretny parametr | en |
| systemd-oom | gleboki mechanizm (nieudokumentowany!) | en |
| k8s-terminating | gleboki mechanizm (KEP) | en |
| dns-ttl | powszechna koncepcja | en |
| mercury | wieloznacznosc (planeta/metal/mitologia) | en |
| eu-vat | fakt regionalny | pl |

## Ocena

1. **Zrodla** — unikalne URL-e z wyjscia omp (ANSI-stripped, deduplikowane).
   Klasy: `PRIMARY` (dokumentacja producenta, kernel, KEP), `QA` (stackoverflow i
   pokrewne), reszta = `unknown` (nie "szum" — nieznane domeny raportujemy osobno).
2. **Trafnosc (fact_hits)** — pobieramy pierwsze 3 zrodla przez curl i szukamy `kluczowych_faktow`
   w tresci + w odpowiedzi dostawcy. `fact_hits` = trafienia/oczekiwane.
   To metryka TEMATYCZNA (obecnosc fraz), NIE merytoryczna.
3. **Merytoryka (judge.py)** — warstwa LLM-as-judge: model orzeka CORRECT /
   INCORRECT / UNSUPPORTED porownujac odpowiedz i zrodla z `oczekiwana_odpowiedz`.
   Rozrozniamy typy: indeksy (synthetic, brave, exa) oceniamy po tym, czy zrodla
   wystarczaja do odpowiedzi; syntetycy (gemini, anthropic) po poprawnosci prozy.
4. **Wariancja** — kazde pytanie x3 proby; report.py pokazuje rozrzut.

## Uzycie warstwy sędziowskiej

```bash
python3 judge.py              # ocen wszystkie nieocenione rekordy
python3 judge.py --provider brave   # tylko jednego dostawce
python3 judge.py --rejudge    # ocen ponownie wszystko
```

Sedzia uzywa ollama-cloud (`omp token ollama-cloud`) — ten sam model co sesja,
nie obciaza limitow Anthropica. Pytania bez `oczekiwana_odpowiedz` (systemd-oom)
sa pomijane — brak wzorca, sedzia nie ma czym rozstrzygnac.

## Znane ograniczenia

- Anthropic bywa pod rate-limit (429) — proba zapisana jako ERROR, nie liczona.
- Gemini maskuje domeny za `vertexaisearch.cloud.google.com` — ocena zrodel
  niemozliwa, ale trafnosc odpowiedzi mierzalna. Gemini uzywa tez cyfr Unicode
  (200.592), normalizowanych do ASCII przed ocena.
- `fact_hits` zalezy od dostepnosci stron (Cloudflare, JS-renderowane strony
  moga nie zawierac faktow w surowym HTML).
- Fetch przez curl z UA przegladarki; czas pobierania wliczony w `sec`.
- UNSUPPORTED u indeksow (synthetic/brave/exa) to nie wina dostawcy — czyste
  indeksy nie produkuja prozy, wiec sedzia nie ma czego ocenic; mierzy tylko
  czy zrodla wystarczaja.
- Sedzia (ollama-cloud) to ten sam model co sesja robocza — mozliwa stromicza
  ocena na korzysc zrodel podobnych do wlasnej wiedzy modelu.
