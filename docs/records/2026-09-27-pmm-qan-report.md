# PMM QAN `metrics:getReport`: błąd wariantów sortowania

**Dla opiekuna monitoringu:** gdy QAN pokazuje dane, lecz zapytanie API o raport zwraca błąd, użyj `order_by: "load"`; nie uznawaj samego HTTP 500 za dowód utraty danych. Ten zapis dotyczy obserwacji w laboratorium `xenonv17` z 2026-09-27, nie potwierdza naprawy w innych instalacjach.

## Objaw i zakres

Na działającym PMM (sonda laboratorium raportowała 3.9.1) `POST /v1/qan/metrics:getReport` z `group_by: "queryid"` i danymi z ostatnich minut:

| Wariant tego samego okresu i filtra | Wynik |
| --- | --- |
| Bez `order_by` | HTTP 500, kod API `13` |
| `order_by: "num_queries"` | HTTP 500, kod API `13` |
| `order_by: "load"` | HTTP 200, rzeczywiste wiersze raportu |

Log `qan-api2` przy błędnych wariantach zawierał panic `index out of range [0] with length 0` w `analytics.(*Service).GetReport`. To obserwacja awarii serwera, nie diagnoza jej przyczyny. `metrics:getFilters` działał. Po ponownej rejestracji monitora PMM wykonano 12 zapytań `SELECT` przez zweryfikowany TLS; raport z `order_by: "load"` zwrócił ich digest z `num_queries: 12` (przed ruchem brakowało tego unikatowego digesta). Zbieranie QAN i uprawnienia monitora działały.

[Oficjalny README `qan-api2`](https://github.com/percona/pmm/blob/main/qan-api2/README.md) podaje przykłady raportu zarówno **bez** `order_by`, jak i z `order_by: "num_queries"`; oba warianty powinny zwrócić raport zamiast błędu wewnętrznego. Dokumentuje to niezgodność obserwowanego zachowania z przykładami upstream. Nie zgłoszono jeszcze osobnego issue upstream.

## Obejście i materiał do zgłoszenia

Wyślij tę samą treść żądania i zmień wyłącznie `order_by` na `"load"`. Do porównania zachowaj identyczny okres, `group_by`, `labels` i `limit`; wybierz zakres, w którym `metrics:getFilters` pokazuje dane. Nie umieszczaj hasła PMM w poleceniu, URL, logu ani treści issue. Zgłoszenie upstream powinno zawierać wyłącznie zanonimizowaną treść żądania, trzy kody odpowiedzi, kod API `13`, sygnaturę panic i informację, że digest z działającego wariantu zawiera dane.
