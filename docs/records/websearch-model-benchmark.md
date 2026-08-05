# Porównanie modeli web search — protokół i wyniki

Cel: rozstrzygnąć empirycznie, który model Gemini daje lepszy grounding, zamiast
opierać się na numerze wersji albo cudzej opinii z issue.

## Zapytanie kontrolne (NIE zmieniać między przebiegami)

```
Does galera-4 26.4.27 mariabackup SST support encrypt=4, and is MDEV-26360 fixed in that version?
```

Wybrane celowo: odpowiedź jest nam realnie potrzebna do dokończenia TLS na
`finalclaude-r9`, jest weryfikowalna w źródłach pierwotnych (MariaDB Jira, docs),
i jest na tyle wąska, że słaby grounding od razu widać.

## Co mierzymy

| kryterium | jak |
|---|---|
| dostawca faktycznie użyty | redirecty `vertexaisearch` = Gemini; bezpośrednie URL-e = ktoś inny |
| liczba źródeł | ile pozycji w `Sources` |
| jakość źródeł | pierwotne (jira.mariadb.org, mariadb.com/docs, kod) vs wtórne (blogi, agregatory) |
| trafność | czy odpowiada na OBA człony pytania: `encrypt=4` ORAZ status MDEV-26360 |
| konkret | czy podaje wersję naprawy, czy tylko ogólniki |

## Wyniki

| model | dostawca | źródeł | pierwotnych | odpowiada na oba człony | uwagi |
|---|---|---|---|---|---|
| `gemini-3.6-flash-high` | **Gemini** | 12 | mieszane | **tak** | 8 podzapytan; wersje naprawcze 5/6 poprawnie |
| `gemini-3.6-flash-medium` | | | | | |
| `gemini-3.6-flash-low` | | | | | |
| `gemini-3-flash` | | | | | goły ID, bez obejścia wire-id |
| `gemini-2.5-flash` | | | | | domyślny, grupa kontrolna |

## Jak zmienić model

```bash
omp config set providers.webSearchGeminiModel <model>   # wymaga restartu sesji
```

## Kontekst pułapki

`gemini-3.6-flash` (gołe ID) zwraca **404** na endpointcie groundingu — to logiczny
identyfikator z katalogu omp, którego ścieżka wyszukiwania nie rozwiązuje na drutowy
(`resolveWireModelId` jest używane tylko w ścieżce czatu). Trzeba podawać `-low`/`-medium`/
`-high`. Porażka jest **cicha**: łańcuch przechodzi do następnego dostawcy i nikt się nie
dowiaduje. Zgłoszenia: can1357/oh-my-pi#6868 (repro), #5300 (widoczność), #7720 (routing).

## Przebieg 1 — `gemini-3.6-flash-high` (2026-08-05)

**Dostawca:** Gemini potwierdzony — redirecty `vertexaisearch` obecne. Obejscie wire-id
z #6868 dziala: gole `gemini-3.6-flash` dawalo 404 i ciche przejscie na Anthropic.

**Wynik:** 12 zrodel, 8 podzapytan, odpowiedz na oba czlony pytania.

**Weryfikacja trafnosci** (bezposrednio w API Jiry, nie na wiare):

```
status: Closed   resolution: Fixed
fixVersions: 10.2.41, 10.3.32, 10.4.22, 10.5.13, 10.6.5, 10.7.1
```

Model podal 5 z 6 wersji naprawczych (pominal `10.7.1`) i poprawnie wyliczyl tryby
`encrypt=0..4`. **Merytorycznie trafne.**

**Jakosc zrodel: mieszana.** Obok pierwotnych (`mariadb.com`, `github.com`) pojawily sie
wtorne — `linuxbabe.com`, `rssing.com`. Przy pytaniu o status zgloszenia lepiej siegnac po
API Jiry niz po synteze.

**Wniosek merytoryczny dla floty:** MDEV-26360 dotyczy 10.2.40-10.6.4. Mamy **11.4.12**,
wiec nas nie obejmuje — `encrypt=3` w szablonie zostaje, `encrypt=4` niepotrzebne.
