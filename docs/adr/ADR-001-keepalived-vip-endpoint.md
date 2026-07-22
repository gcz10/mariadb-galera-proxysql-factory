# ADR-001: Redundantny endpoint ProxySQL — Keepalived VIP

**Data:** 2026-07-22
**Status:** Accepted
**Decydent:** Principal (Interview 2026-07-22)

## Kontekst

Klienci aplikacyjni potrzebują jednego stabilnego endpointu do ProxySQL, który przetrwa awarię jednej instancji ProxySQL. Trzy opcje zostały rozważone:

1. **External Load Balancer** (F5/HAProxy zewn.) — HA poza węzłami klastra
2. **Keepalived VIP** — VIP漂移 między 2 węzłami ProxySQL przez VRRP
3. **DNS** (round-robin / weighted TTL) — najprostsze, ale najwolniejsze przełączenie

## Decyzja

**Keepalived VIP na węzłach ProxySQL.**

## Uzasadnienie

- Brak zewnętrznej zależności (LB) — VIP żyje na węzłach ProxySQL
- Szybkie przełączenie (VRRP < 3s) — spełnia RTO węzła <2 min (ISC-25)
- Health-check ProxySQL przez Keepalived odrzuca niesprawną instancję (ISC-26)
- Koszt: zero dodatkowej infrastruktury
- Wymaga: osobne CIDR dla VIP, rekomendacja anti-affinity w vCenter (nie implementujemy walidacji vCenter — Out of Scope)

## Konsekwencje

- VIP jest zasobem na węzłach klastra (zaleta: brak zewn. zależności; obciążenie: VRRP na węzłach)
- `secrets.example.yml` zawiera `keepalived.vrrp_pass` (Ansible Vault)
- ISC-24, ISC-25, ISC-26 zależą od tej decyzji
- F8 implementuje konfigurację Keepalived

## Odrzucone warianty

- **External LB:** czystsze rozdzielenie, ale wymaga zewn. infrastruktury LB i zależności operacyjnej
- **DNS:** najprostsze, ale TTL cache klientów wydłuża RTO ponad uzgodniony próg 2 min
