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

## Przebiegi 4-5 — Exa i Brave (2026-08-05)

Zapytanie kontrolne identyczne, przez `omp search --provider <x>`.

### Zbiorcze porownanie wszystkich dostawcow

| dostawca | zrodla | trafione zgloszenia Jira | unikalne znalezisko |
|---|---|---|---|
| Gemini `-high` | 12 | **0** | — |
| Gemini `-low` | 9 | **0** | *(zmyslony tytul + zly config)* |
| Anthropic haiku-4-5 | 37 | 18050, 25359, 15910 | szerokosc |
| Anthropic sonnet-5 | 37 | te same | nic ponadto, 3,5x koszt wyjscia |
| **Exa** (z kluczem) | 10 | 26360, 18050, **30402** | **plik zrodlowy** `wsrep_sst_mariabackup.sh` |
| **Brave** | 10 | 18050, 26360, 25359, 15910, **27181** | regresja MDEV-27181 |

### Wniosek

Liczba zrodel nie przewiduje jakosci. Exa i Brave przy 10 pozycjach trafily w wiecej
zgloszen niz Gemini przy 12 — Gemini **nie dotknal ani jednego ticketu** w zadnym z dwoch
przebiegow, cytujac dokumentacje i blogi opisujace problem.

Kazdy z trzech dobrych dostawcow znalazl cos, czego nie znalazl zaden inny. Pelny obraz dala
dopiero **suma**:

| zgloszenie | rzecz | naprawione | znalazl |
|---|---|---|---|
| MDEV-18050 | wprowadzil `encrypt=4` | 10.2.40+ | Anthropic, Exa, Brave |
| MDEV-26360 | `encrypt=3` + nazwy hostow | 10.6.5 | Exa, Brave |
| MDEV-27181 | regresja z poprawki 26360 | 10.6.6 | **tylko Brave** |
| MDEV-30402 | socat 1.7.4 SNI psuje `encrypt=4` | 11.1.1 | **tylko Exa** |

Wszystkie ponizej naszego 11.4.12.

### Uwaga o Exa bez klucza

Pierwszy test Exa poszedl bez klucza, przez publiczny MCP (`mcp.exa.ai`), i zwrocil dwa
ogolne dokumenty bez zadnego zgloszenia. Wyciagnalem z tego wniosek, ze semantyka nie nadaje
sie do wyszukiwania po identyfikatorach — **bledny**. Z kluczem (`api.exa.ai`) Exa trafila
w MDEV-26360 od razu. Sciezka bezkluczowa jest zdegradowana i nie nadaje sie do oceny.

### Ustawiona kolejnosc

```
["anthropic","gemini","brave","exa","public"]
```

Uzasadnienie po pomiarze: Anthropic ma najwieksza szerokosc, Gemini najlepsza synteze przy
najmniejszym koszcie, Brave i Exa sa tanie i trafiaja w zrodla pierwotne, `public` to
konsensus czterech darmowych indeksow jako ostatnia deska ratunku.

---

# Runda 2 — trzy pytania roznego typu (2026-08-05)

Pierwsza runda uzyla jednego waskiego pytania z identyfikatorem (`MDEV-26360`), co
faworyzowalo indeksy slow kluczowych. Ta runda sprawdza trzy typy naraz.

## Pytania

| id | pytanie | gdzie realnie lezy odpowiedz |
|---|---|---|
| Q1 | flow control Galery: co powoduje narastanie `wsrep_local_recv_queue`, jak stroic `gcache.size` i `gcs.fc_limit` | docs Codership, blogi Percony |
| Q2 | dlaczego `Restart=always` nie zadziala po OOM kill i co zmienia `OOMPolicy` pod cgroup v2 | `systemd.service(5)`, kod systemd, lista jadra |
| Q3 | dlaczego pod StatefulSet zostaje w `Terminating` przy wezle NotReady; co zmienia KEP-2268 | KEP, issues k8s, kubernetes.io |

Q2 i Q3 dobrane tak, ze popularna odpowiedz blogowa jest niepelna — wymagaja specyfikacji.

## Wynik

| # | dostawca | zrodla pierwotne | Q&A | szum | sr. czas | sr. zrodel |
|---|---|---|---|---|---|---|
| 1 | **synthetic** | **100%** (6/6) | 0 | 0 | **2,3 s** | 5 |
| 2 | **anthropic** | 53% (8/15) | 0 | 7 | 22,1 s | 37 |
| 3 | **exa** | 54% (6/11) | 3 | 2 | 9,9 s | 10 |
| 4 | **brave** | **25%** (4/16) | 4 | 8 | 2,1 s | 10 |
| 5 | gemini | **nieocenialne** | — | — | 17,6 s | 14 |

## Najwazniejsze: Brave spadl z 1. na 4. miejsce

W rundzie 1 (waskie pytanie z ID) Brave trafil w 5 zgloszen Jira i wygral. Tutaj zwraca
`dohost.us`, `devops.aibit.im`, `readme.phys.ethz.ch`, `michal-drozd.com`.

To nie sprzecznosc, tylko dwa zadania:
- **znajdz konkretny dokument** → indeks slow kluczowych wygrywa → **Brave**
- **wyjasnij mechanizm** → potrzebna selekcja zrodel → **Synthetic, Anthropic**

## Pozostale ustalenia

**Synthetic** utrzymal 100% na wszystkich trzech: `mariadb.com`; `cdn.kernel.org` +
`freedesktop.org` + `github.com`; `kubernetes.io` + `github.com`. Zero blogow.
**Limit nieznany** — API nie zwraca naglowkow, saldo tylko w panelu. Pozycja 1 jedzie
na niezweryfikowanym limicie.

**Anthropic** ma najglebszy zasieg — jako jedyny dotarl do `lkml.iu.edu` (archiwum listy
jadra). Cena: 37 zrodel, polowa to forum nvidii i przypadkowe wpisy, 22 s, limit uzytkownika.

**Gemini maskuje wszystkie domeny** za `vertexaisearch.cloud.google.com`. Nie da sie ocenic
doboru zrodel bez rozwijania kazdego przekierowania. Przy pracy opartej na weryfikacji
zrodel to dyskwalifikuje go z pierwszej pozycji, niezaleznie od jakosci syntezy.

## Dostawcy wykluczeni

| dostawca | powod |
|---|---|
| `codex` | **wisi pelne 60 s** i nie zwraca nic; stoi na pozycji 4 domyslnego lancucha |
| `zai` | `MCP error -429: Weekly/Monthly Limit Exhausted`, reset 2026-08-07 |
| `public` | timeout > 120 s (startuje headless Chromium mimo obiecanego limitu 30 s) |

## Konfiguracja koncowa

```
providers.webSearchOrder   ["synthetic","brave","exa","anthropic","gemini"]
providers.webSearchExclude ["codex","zai","public"]
```

Zweryfikowane na zywo: zapytanie bez wymuszania dostawcy trafia do Synthetic w 3,4 s.

## Zastrzezenia

- **n = 3 pytania.** Kierunek wyrazny, ale to nie proba statystyczna.
- **Ocena po domenie, nie po tresci** — link do `github.com` moze byc repozytorium systemd
  albo czyims gistem.
- **Nie weryfikowano poprawnosci odpowiedzi** na tych trzech pytaniach. Mierzony jest
  **dobor zrodel**, nie trafnosc. W rundzie 1 trafnosc sprawdzano wobec API Jiry.
