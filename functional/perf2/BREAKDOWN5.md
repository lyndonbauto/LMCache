# kv-sink breakdown, day 2 round 4 (Sriram, why the send task idles, 2026-10-06)

Sriram's follow-up to `BREAKDOWN4.md` (Slack, 2026-10-06 13:19 PT): why each QP's send
task idles ~63% of the time for the sink with 8 writes queued per QP. Same builds as
rounds 2-3: asd `0c9703931` at defaults (32 placers, cap 128), `kvlayers` on client
`e8158149`, memory namespace, `KV_SINK_STATS=1`, LMCache unchanged; 16 QPs, 512 KiB.

Configs: P8 (`ib_write_bw -q 16 -t 8`) and S (`kvlayers --qps 16`). Each runs twice
for 25 s with a 10 s window starting 3 s in:

- `-A`: `rdma statistic show link rxe0/1` right before and after the window.
- `-B`: the same counters around Sriram's bpftrace script (`rxe_requester` exit reasons,
  retransmit and RNR timers), so bpftrace's overhead stays out of A. It costs ~2%
  (S 7.28 -> 7.15 GiB/s; P8 within run-to-run noise).

Changes to Sriram's script, all forced by this box:

- The rxe module (our v6.11 build, `/root/rxe-build/v6.11/rdma_rxe.ko`) has no BTF, so
  the flags are read by offset from the module's debug info (`pahole`, saved in
  `breakdown5/pahole_*.txt`): `qp->req.wait_psn` at 1136, `qp->need_req_skb` at 1856.
  The script checks that the loaded module's `srcversion` matches the `.ko` first.
- `rxe_retransmit_timer` doesn't exist in v6.11; the timer is `retransmit_timer`.
- `nothing_or_other` is split further: `qp->req.wait_fence` (1128) and
  `qp->req.need_rd_atomic` (1132), checked after Sriram's two flags.
- GiB in the bpftrace window = `sent_packet` exits / 262,144. Each `rxe_requester`
  call that returns 0 sends one 4 KiB data packet (a 512 KiB write is 128).

bpftrace 0.20.2 was already installed on the box, so nothing was installed or removed.

- Script: `scripts/breakdown5.sh`; tables: `scripts/breakdown5_report.py`.
- Outputs: `breakdown5/report.md`, per run `counters_before.txt` /
  `counters_after.txt` / `bpftrace.json`, `exits.bt` (the program as run),
  `S_server/stats_lines.txt`, and `run2_report.md` (a repeat without the fence split).
- A first pass used 15 s runs. bpftrace takes ~4 s to attach, so its 10 s ran past the
  end of the run. It was discarded (kept on the box as `breakdown5_run1/`), though
  its exit mix matched.

## Result

No retransmits, no RNR, no sequence errors: every error counter is 0 in both configs,
and both timers fired 0 times. Packets per GiB (265.7-267.9k) and deferred ACKs per GiB
(~4,090) are the same for S and P8. So the wire protocol behaves identically.

The requester's stops differ in one way. For S, it finds nothing to send 1,830 times per
GiB, about 0.9 times per 512 KiB write; for P8, 5 per GiB. Fence and read-atomic waits are
0, so this is the send queue running dry (or a QP-state exit). Window-full stops are rare
for both (S 500 / GiB, P8 187), and the receiver-backed-up stop (>64 skbs in flight) is
the most common for both, slightly less for S (6,637 vs 7,926 per GiB).

By Sriram's reading, this is "our writes don't reach the QPs steadily". Most QPs run dry
about once per write, so a QP usually has no unsent write queued behind the one on the wire,
although the server reports 8 writes in flight per QP. One reading, not tested: the
server's "in flight" counts a write from admission until it sees the completion. That
would include the placer wait (~1.3 ms), the copy, and completion reaping, not only the
time the write sits in the send queue. Then fewer writes are actually queued per QP than 8.

Possible next step (not run): sample each QP's send-queue depth (producer minus
consumer index) at `rxe_requester` entry for S and P8. Also compare the server's
post-to-completion time with the time from first to last packet of each write.

## Tables

A. Soft-RoCE counter deltas, 10 s windows (run 3; run 2 within 1%):

```
counter              P8-A       /GiB   P8-B       /GiB   S-A        /GiB   S-B        /GiB
GiB in window        108.6             148.2*            72.6              95.9*
sent_pkts            29102523  267870  39428916  266041  19294050  265716  25507308  265898
rcvd_pkts            = sent_pkts (within 2)
completer_retry_err  0                 0                 0                 0
retry_exceeded_err   0                 0                 0                 0
out_of_seq_request   0                 0                 0                 0
rcvd_seq_err         0                 0                 0                 0
duplicate_request    0                 0                 0                 0
ack_deferred         447730    4121    606598    4093    296832    4088    392430    4091
rcvd_rnr_err         0                 0                 0                 0
send_rnr_err         0                 0                 0                 0
```

\* B windows are ~13.8 s (counters around bpftrace's attach, 10 s count and exit).

B. `rxe_requester` exits, 10 s (run 3; run 2 gives S 1,867 / GiB nothing_or_other,
P8 4):

```
exit               P8-B count  /GiB     %       S-B count  /GiB     %
sent_packet         27691850  262144  97.00     18109569  262144  96.69
window_full            19749     187   0.07        34569     500   0.18
rx_backed_up          837292    7926   2.93       458526    6637   2.45
wait_fence                 0       0   0.00            0       0   0.00
need_rd_atomic             0       0   0.00            0       0   0.00
nothing_or_other         523       5   0.00       126426    1830   0.68
calls               29040190  274908            19026104  275411
retransmit timer           0                           0
rnr timer                  0                           0
GiB (window)           105.6 (10.56 GiB/s)          69.1 (6.91 GiB/s)
```
