# kv-sink breakdown, day 2 round 8 (Sriram, per-QP depth sweep, 2026-10-06)

Sriram's follow-up to `BREAKDOWN8.md` (Slack, 2026-10-06 16:39 PT). His reading: priority
is enforced only in the server queue, and a QP sends what was posted to it in FIFO order.
The post lock kept per-QP backlogs shallow. Without it, every admitted write lands on a
QP at once, so layer 0 can queue behind other layers. This round sweeps the per-QP depth
on the no-lock build to see what a shallow backlog does to layer 0 and to throughput.

- Binaries: old = asd `3b1d52fb3` (post lock), new = asd `55d6ae8d8` (no post lock), both
  in their own directories. `kvlayers` on client `e8158149`, memory namespace, 16 QPs,
  512 KiB writes, 32 placers, `KV_SINK_STATS=1`, LMCache unchanged.
- At 16 QPs, writes per QP = `KV_SINK_PATH_BUDGET_MB` / 512 KiB, so the budget and the
  in-flight cap are the same lever:

  | point | binary | env | per QP | in flight |
  |---|---|---|---|---|
  | 01-old-def | old | defaults | 8 | 128 |
  | 02-new-B1 | new | `KV_SINK_PATH_BUDGET_MB=1` | 2 | 32 |
  | 03-new-B2 | new | `KV_SINK_PATH_BUDGET_MB=2` | 4 | 64 |
  | 04-new-def | new | defaults (4 MiB) | 8 | 128 |
  | 05-new-C32 | new | `KV_SINK_MAX_IN_FLIGHT=32` | 2 | 32 |
  | 06-old-def2 | old | defaults | 8 | 128 |

- Each point is one server cycle, in round 3's order: LMCache stores prompts 0-3
  (`precheck qpstore:8192`), `kvlayers --fill --reps 1`, then `kvlayers --qps 16
  --duration 25` with mpstat for 10 s starting 3 s in. Then lw 8k c=1 at 16 QPs
  (`timeline:16`), with the E3 per-layer print patch applied and reverted, and the tree
  checked clean before and after.
- Script: `scripts/breakdown9.sh`; tables: `scripts/breakdown9_report.py`.
- Outputs: `breakdown9/report.md`, `breakdown9/breakdown9.out`; per point `point.txt`,
  `stats_lines.txt`, `clean/` (kvlayers, mpstat, window times), `E_timeline_qp16/` (lw
  session) and `L8192_aon/` (the store step).

## Result

Nothing on the new build beats the old one. By Sriram's reading that points to reverting
to locked posting for now.

- Throughput: old 7.53 / 7.48 GiB/s (first / last, so no drift). New: 6.41 (B1), 6.71
  (B2), 6.82 (defaults), 6.22 (C32). Shallower is slower, and every new point is below old.
- lw TTFT p50: old 0.177 / 0.179 s. New: 0.226 (B1), 0.214 (B2), 0.203 (defaults), 0.234
  (C32). Best new is 14% slower than old.
- Layer 0 arrival: shallow depth does bring it earlier: new 11.0 ms (B1), 20.8 (B2),
  21.6 (defaults), 12.6 (C32), against old 24.0 / 29.7 ms. But layer 31 lands later
  (161-186 ms vs 138-139 ms for old), and TTFT follows layer 31.
- The two levers agree: B1 and C32 both hold 32 in flight and give the same wire time
  (2.15 / 2.18 ms), placer queue (2.2 / 2.3) and layer 0 (11.0 / 12.6 ms). C32 is 3%
  slower (6.22 vs 6.41 GiB/s).
- Placers aren't the limit on the new build: placer-wait is 0.2-0.46 ms and the placer
  queue is 2-4, against old's 0.9-1.2 ms and 14-19. Busy QPs are 15.7-16 of 16 at every
  point, and CPUs busy are lower on new (7.7-8.3 vs 9.4-9.6).
- Round 7's late layer 0 (+70 ms) was a depth effect: that lw run used N512 (32 per QP).
  At the default 8 per QP the new build's layer 0 is at +21.6 ms, about where old is.

So a shallow backlog trades throughput for an earlier layer 0, but it doesn't help lw: the
whole fetch gets slower, and TTFT with it. The old build's bytes per second is what's
missing. That fits round 7's unmeasured reading that concurrent posts cost more rxe work
per byte, but this round doesn't measure it.

## Tables

`kvlayers` GiB/s over 25 s; stats GiB/s is the mean of the stats lines in the 10 s window;
stat fields are from the highest-rate line in the window (wire and placer-wait in us per
write); layer 0 / 31 are the median ms after `begin_fetch` at which that layer is resident.

| point | kvlayers GiB/s | stats GiB/s | in flight | placer-wait | wire | busy qps | placer queue | CPUs busy | lw TTFT p50 s | layer 0 ms | layer 31 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 01-old-def | 7.53 | 7.51 | 128 | 929 | 7174 | 16.0 | 13.9 | 9.4 | 0.177 | 24.0 | 137.7 |
| 02-new-B1 | 6.41 | 6.45 | 32 | 199 | 2146 | 15.7 | 2.2 | 7.7 | 0.226 | 11.0 | 185.8 |
| 03-new-B2 | 6.71 | 6.73 | 64 | 286 | 4189 | 15.9 | 2.8 | 7.9 | 0.214 | 20.8 | 175.4 |
| 04-new-def | 6.82 | 6.86 | 128 | 427 | 8265 | 16.0 | 4.2 | 8.3 | 0.203 | 21.6 | 161.2 |
| 05-new-C32 | 6.22 | 6.17 | 32 | 212 | 2184 | 15.7 | 2.3 | 7.7 | 0.234 | 12.6 | 184.9 |
| 06-old-def2 | 7.48 | 7.50 | 128 | 1217 | 6831 | 16.0 | 18.6 | 9.6 | 0.179 | 29.7 | 138.6 |

The two steady stats lines per point are in `breakdown9/report.md`.
