#!/usr/bin/env python3
"""ISC-68: policz `gcache.size` PRZED budowa, z jawnego write rate.

DLACZEGO TO ISTNIEJE: ISA.md opisuje lancuch "zmierz -> policz -> zweryfikuj",
gdzie mierzy `tests/lab/probe-gcache.py`, a weryfikuje brama po budowie. Srodek
- ten skrypt - byl w dokumentacji wymieniony, ale w repozytorium go NIE BYLO.
Skutkiem bylo, ze jedyna droga do poznania wymaganej wartosci prowadzila przez
pietnastominutowa budowe zakonczona czerwona bramka: pomiar byl jednoczesnie
pierwszym wykryciem problemu.

Formula (Galera docs, ISC-68):

    gcache.size = write_rate_bytes_per_s x ist_window_min x 60

Ponizej 128M nie schodzimy: mniejszy bufor nie pokrywa nawet krotkiego restartu,
a `galera.cache` to plik na dysku, wiec oszczednosc jest pozorna.

Wezel wracajacy po awarii dostaje IST (sam przyrost) tylko wtedy, gdy dawca ma
jeszcze w buforze wszystkie write-sety z czasu jego nieobecnosci. Gdy bufor sie
przekrecil - jest SST, czyli pelna kopia bazy przez `mariabackup`, obciazony
dawca i minuty zamiast sekund.

Uzycie:
    tests/validation/calc-gcache.py --write-rate 83500            # 30 min okna
    tests/validation/calc-gcache.py --write-rate 83500 --window 60
    tests/validation/calc-gcache.py --write-rate 83500 --format yaml

Write rate bierzesz z `probe-gcache.py` na dzialajacym klastrze albo z
`f0_discovery` na maszynach, ktore juz obsluguja ruch produkcyjny.
"""

import argparse
import math
import sys

FLOOR_MB = 128
DEFAULT_WINDOW_MIN = 30


def required_mb(write_rate_bytes_s: int, ist_window_min: int = DEFAULT_WINDOW_MIN,
                floor_mb: int = FLOOR_MB) -> int:
    """Zwroc wymagany rozmiar gcache w MB dla podanego write rate i okna IST."""
    if write_rate_bytes_s < 0:
        raise ValueError("write rate nie moze byc ujemny")
    if ist_window_min <= 0:
        raise ValueError("okno IST musi byc dodatnie")
    needed = write_rate_bytes_s * ist_window_min * 60
    return max(math.ceil(needed / (1024 * 1024)), floor_mb)


def covered_rate_bytes_s(gcache_mb: int, ist_window_min: int = DEFAULT_WINDOW_MIN) -> int:
    """Odwrotnosc: jaki write rate pokrywa dany bufor w danym oknie."""
    if ist_window_min <= 0:
        raise ValueError("okno IST musi byc dodatnie")
    return int(gcache_mb * 1024 * 1024 / (ist_window_min * 60))


def main() -> int:
    parser = argparse.ArgumentParser(description="Policz gcache.size dla okna IST")
    parser.add_argument("--write-rate", type=int, required=True,
                        help="zmierzony write rate w bajtach na sekunde")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_MIN,
                        help=f"okno IST w minutach (domyslnie {DEFAULT_WINDOW_MIN})")
    parser.add_argument("--floor", type=int, default=FLOOR_MB,
                        help=f"dolna granica w MB (domyslnie {FLOOR_MB})")
    parser.add_argument("--format", choices=("plain", "yaml"), default="plain",
                        help="plain: sama liczba z jednostka; yaml: linia do cluster.yml")
    args = parser.parse_args()

    try:
        mb = required_mb(args.write_rate, args.window, args.floor)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if args.format == "yaml":
        print(f'  gcache_size: "{mb}M"   # write_rate={args.write_rate}B/s, okno IST {args.window} min')
    else:
        print(f"{mb}M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
