#!/usr/bin/env python3
"""
gcache calculator — wylicza gcache.size z mierzonego write rate i okna IST (ISC-68).

Formuła:
  gcache.size = write_rate_bytes_per_second × ist_window_minutes × 60

Uruchomienie po F0 (gdy write_rate zmierzony):
  python3 tests/validation/calc-gcache.py <write_rate_bytes_per_sec> <ist_window_minutes>

Output:
  gcache.size w MB (zaokrąglone) + rekomendacja do zapisania w Decisions.

Jeżeli write_rate unknown (pusty klaster), zwraca fog zamiast wartości.
"""
import sys
import math


def calc_gcache(write_rate_bps: float, ist_window_min: int) -> dict:
    """Wylicz gcache.size z write rate i okna IST."""
    if write_rate_bps <= 0:
        return {
            "status": "fog",
            "reason": "write_rate = 0 or unknown — cannot calculate gcache without measured workload",
            "gcache_mb": None,
        }

    gcache_bytes = write_rate_bps * ist_window_min * 60
    gcache_mb = math.ceil(gcache_bytes / (1024 * 1024))

    # Minimum bezpieczne: 128MB (nawet dla bardzo małego write rate)
    gcache_mb = max(gcache_mb, 128)

    return {
        "status": "calculated",
        "write_rate_bps": int(write_rate_bps),
        "ist_window_min": ist_window_min,
        "gcache_bytes": int(gcache_bytes),
        "gcache_mb": gcache_mb,
        "recommendation": f"gcache.size={gcache_mb}M",
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: calc-gcache.py <write_rate_bytes_per_sec> <ist_window_minutes>", file=sys.stderr)
        print("  If write_rate unknown, pass 0 to get fog status.", file=sys.stderr)
        return 2

    try:
        write_rate = float(sys.argv[1])
        ist_window = int(sys.argv[2])
    except ValueError:
        print("FAIL: invalid numeric arguments", file=sys.stderr)
        return 1

    result = calc_gcache(write_rate, ist_window)

    if result["status"] == "fog":
        print(f"FOG: ISC-68 — {result['reason']}")
        print("  Must resolve: measure write rate in F0 with representative workload")
        return 0  # fog is not a failure — it's an honest unknown

    print("PASS: ISC-68 — gcache.size calculated")
    print(f"  write_rate: {result['write_rate_bps']} bytes/sec")
    print(f"  ist_window: {result['ist_window_min']} minutes")
    print(f"  gcache_bytes: {result['gcache_bytes']}")
    print(f"  gcache_mb: {result['gcache_mb']}")
    print(f"  recommendation: {result['recommendation']}")
    print(f"  Decision entry: gcache.size={result['gcache_mb']}M — derived from write_rate={result['write_rate_bps']}B/s × {result['ist_window_min']}min × 60")
    return 0


if __name__ == "__main__":
    sys.exit(main())
