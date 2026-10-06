# kv-sink breakdown, day 2 round 5 (Sriram, is the in-flight cap the limit, 2026-10-06)

Sriram's follow-up to `BREAKDOWN5.md` (Slack, 2026-10-06 14:00 PT): does the server's
admission limit starve the QPs? Server asd `3b1d52fb3` (`sriram/kv-sink-batch-prio`):
`KV_SINK_MAX_IN_FLIGHT` now allows up to 512 (staging 2 MiB a slot), and the new
`KV_SINK_PATH_BUDGET_MB` sets the byte budget per QP (default 4). `kvlayers` on client
`e8158149`, memory namespace, 16 QPs, 512 KiB, 32 placers (default), `KV_SINK_STATS=1`,
LMCache unchanged.

Sriram's run table didn't come through the Slack API, so the default set from the
thread ran (no reply within 15 min). C = `KV_SINK_MAX_IN_FLIGHT`, B =
`KV_SINK_PATH_BUDGET_MB`:

- C128-B4: today's defaults, the baseline.
- C256-B4: the cap alone. The budget of 4 MiB x 16 QPs = 64 MiB = 128 writes still binds.
- C256-B8, C512-B16: the cap and the budget raised together.

Each config is one server cycle with one fill, then two `kvlayers --qps 16 --duration 25`
runs with a 10 s window 3 s in. The clean run has mpstat; the bt run has round 4's
bpftrace exit counts (`breakdown5/exits.bt`, same offsets, loaded module checked). C512
started and ran with 0 failed rows.

- Script: `scripts/breakdown6.sh` (`build` as in `breakdown3.sh`, then `runs`); tables:
  `scripts/breakdown6_report.py`.
- Outputs: `breakdown6/report.md`, per config `stats_lines.txt`, `kvl_fill.txt`,
  `clean/` (`kvl.txt`, `mpstat.txt`) and `bt/` (`kvl.txt`, `bpftrace.json`).

## Result

Admission is part of it, but not most of the gap:

- Raising the cap alone changes nothing (C256-B4 = C128-B4), because the byte budget still
  holds in flight at 128.
- Raising both lets more writes in (256 / 512), and nothing-to-send halves and then
  thirds (1,878 -> 904 -> 622 per GiB). GiB/s rises only 6.5% / 8% (7.52 -> 8.01 ->
  8.14), still 75% of raw `ib_write_bw` (10.9).
- The extra writes mostly wait for a placer. The placer queue grows 14 -> 80 -> 302 and
  placer-wait 1.1 -> 4.8 -> 17 ms, with 16.0 of 16 QPs busy and starved 0% throughout.
  Wire time per write grows 6.9 -> 10.3 -> 12.3 ms, so writes also queue longer on each QP.
  CPUs busy grows 9.5 -> 10.5 -> 11.0.

So past ~256 writes the limit moves to the placers. At ~16,900 writes/s, 32 placers handle
~530 writes/s each, which is ~1.9 ms per write. The stats line shows only ~75 us of that
(read 1-2, copy 54-76, post 0-1). The rest is time a placer holds a write outside those
three steps. Not measured; it's the next place to look (for example, whether a placer
waits for the path budget or a QP slot after it picks a write).

By Sriram's reading: GiB/s rises and nothing-to-send falls as the cap grows, so the full
cap does starve the QPs. Freeing it moves the limit to placer throughput, not to the
wire.

## Tables

1. Clean runs. GiB/s from `kvlayers` over 25 s and from the server's stats lines in the
   10 s window; stats from the highest-rate line in the window (3 lines per config in
   `breakdown6/report.md`); CPUs busy from mpstat (10 s).

```
config    kvlayers  stats  in flight  placer-wait us  wire us  busy qps  placer queue  CPUs
C128-B4   7.52      7.48   128        1119            6913     16.0/16   13.8          9.5
C256-B4   7.56      7.54   128         996            7090     16.0/16   13.9          9.5
C256-B8   8.01      8.02   256        4754           10345     16.0/16   79.8          10.5
C512-B16  8.14      8.15   512       17284           12291     16.0/16  301.5          11.0
```

2. `rxe_requester` exits per GiB (bt runs, 10 s; GiB = `sent_packet` / 262,144).

```
exit              C128-B4  C256-B4  C256-B8  C512-B16
sent_packet        262144   262144   262144   262144
window_full           500      514      326      280
rx_backed_up         6629     6612     7328     7517
wait_fence              0        0        0        0
need_rd_atomic          0        0        0        0
nothing_or_other     1878     1886      904      622
GiB/s (bt window)    7.23     7.09     7.62     7.86
```

C128-B4 repeats round 4's S (nothing_or_other 1,830-1,867 per GiB).
