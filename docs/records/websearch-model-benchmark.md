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
| `gemini-3.6-flash-low` | **Gemini** | 9 | mieszane | tak | 5 podzapytan; **zmyslony tytul zgloszenia + bledny przyklad configu** |
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

## Przebieg 2 — `gemini-3.6-flash-low` (2026-08-05)

**Dostawca:** Gemini (redirecty obecne). **Wynik:** 9 zrodel, 5 podzapytan.

Oba czlony pytania trafione, wersje naprawcze te same co przy `-high` (5/6, tez bez 10.7.1).

### Dwa bledy, ktorych `-high` nie popelnil

**1. Zmyslony tytul zgloszenia.** `-low` podal:

> *"MariaDB Enterprise Cluster joiner node incorrectly uses localhost for TLS certificate
> verification and fails to join cluster when wsrep_sst_method=mariadb-backup..."*

Faktyczny tytul z API Jiry: **"Using hostnames for MariaBackup SSTs breaks certificate
validation with encrypt=3"**. Parafraza brzmi jak cytat i jest podana w cudzyslowie —
najgorszy wariant, bo wyglada na weryfikowalna.

**2. Bledny przyklad konfiguracji.** `-low` wygenerowal blok dla `encrypt=4` z parametrami
`tcert`/`tkey`/`tca` — a to sa parametry `encrypt=3`. `encrypt=4` uzywa `ssl-cert`/`ssl-key`/
`ssl-ca`. Sam sobie zaprzeczyl w tym samym akapicie ("standard server SSL options
(`ssl-key`, `ssl-cert`, `ssl-ca`)" w opisie trybu, a `tcert/tkey/tca` w przykladzie).
Ktos, kto skopiowalby ten blok, dostalby niedzialajaca konfiguracje.

## Werdykt

| | `-high` | `-low` |
|---|---|---|
| podzapytania | 8 | 5 |
| zrodla | 12 | 9 |
| trafnosc merytoryczna | poprawna | poprawna |
| **konfabulacje** | **brak** | **tytul zgloszenia + przyklad configu** |

Roznica nie jest kosmetyczna. Przy `-low` wiecej twierdzen powstaje z modelu zamiast ze
zrodel — a wygladaja identycznie wiarygodnie. Przy pracy, gdzie kopiuje sie konfiguracje
z odpowiedzi, to realne ryzyko.

**Rekomendacja: `-high`.** `-medium` nietestowany — roznica high/low jest na tyle wyrazna,
ze schodzenie ponizej `-high` nie ma uzasadnienia przy naszym zastosowaniu.

## Przebieg 3 — Anthropic `claude-haiku-4-5` (2026-08-05)

**Uwaga metodologiczna:** uruchomione przez CLI `omp search --provider anthropic`, nie przez
narzędzie `web_search` w sesji (łańcuch i tak wybrałby Gemini z pozycji 2). Zapytanie
kontrolne identyczne. Z CLI widać tylko ogon odpowiedzi, więc ocena opiera się na **zbiorze
źródeł**, nie na pełnej treści.

| | Gemini `-high` | Anthropic `haiku-4-5` |
|---|---|---|
| źródła | 12 | **37** |
| wyszukiwania | 8 podzapytań | 4 |
| typ źródeł | dokumentacja + blogi (`linuxbabe`, `rssing`, `thzhost`) | **zgłoszenia Jira** + dokumentacja |
| koszt | plan Google | `in 2563 · out 723 · search 4` z limitu Anthropic |

### Co przesądziło

Anthropic znalazł **MDEV-18050 „Port encrypt=4 from xtrabackup-v2 to mariabackup for SSTs"** —
zgłoszenie, które *wprowadziło* `encrypt=4`. Zweryfikowane: `Closed/Fixed`, wersje
10.2.40–10.7.1. To jest źródło pierwotne odpowiedzi na pierwszy człon pytania; Gemini
odpowiedział poprawnie, ale cytując opisy trybów, nie ticket.

Dodatkowo Anthropic wyciągnął MDEV-25359, MDEV-15910, a przy węższym pytaniu — MDEV-27181,
który ujawnił, że poprawka MDEV-26360 wprowadziła regresję w tych samych wersjach. **Żaden
przebieg Gemini tego nie pokazał.**

### Wzorzec

- **Gemini** streszcza to, co *napisano o* problemie — dokumentacja, poradniki, agregatory.
- **Anthropic** trafia w to, *gdzie problem rozstrzygnięto* — zgłoszenia, commity.

Przy weryfikacji twierdzeń wobec źródeł pierwotnych to różnica jakościowa. Przy szerokim
rozpoznaniu tematu Gemini daje więcej kontekstu za darmo.

### Zastrzeżenia

Po jednym zapytaniu na dostawcę — mocna przesłanka, nie dowód. Koszt Anthropic idzie
z limitu użytkownika (`search 4` = cztery płatne wyszukiwania); Gemini w tym sensie jest
darmowy.

## Dlaczego darmowe silniki nie startują

DuckDuckGo, Ecosia, Google i Mojeek **działają** (sprawdzone przez `omp search --provider X`),
ale łańcuch jest **sekwencyjny i kończy się na pierwszym sukcesie** — Gemini stoi na pozycji 2
i odpowiada, więc pozycje 3–23 są osiągalne, lecz nieosiągane. Startpage padł na
bot-challenge (udokumentowane zachowanie, `SearchProviderError 429`).

Rola darmowych silników to siatka bezpieczeństwa na wypadek awarii wszystkich płatnych,
a nie „nigdy nieużywane".
