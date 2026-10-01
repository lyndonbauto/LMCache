# Stage 3 (pipelined path on Soft-RoCE): summary

Server under test: the kv-sink server `512b0c207`. It is a fencing build
(issue 4), so every pipelined row is "pass on a fencing server" and is rerun
when a no-fence build lands. The GPU sections are prepared but have not run;
see [HARNESS.md](HARNESS.md).

## CPU half (cpu-prep-s3s5, 2026-10-01)

| Test | Result | Evidence (box: `/root/lmc-work/functional/stage3/logs/rdma06/`) |
|---|---|---|
| T-RDMA-06, fetches of at most 4 chunks | **pass** 100/100 keys byte-identical, in 4 out of 4 runs | `run1.txt`..`run4.txt`, `run*.junit.xml` (`test_rdma_equals_plain_gets_for_100_p_exact_keys_in_fetches_of_4_chunks`) |
| T-RDMA-06, fetches over 4 chunks (the P-exact-15 64-chunk prompt) | **blocked by D-12** (fell back in 2 out of 2 runs) | `run1.txt`, `run4.txt` (`test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys`); `asd-kvsink_run1.log`/`_run4.log`: 1 `late completion for slot 256` each, then 3775 / 3519 `region N in error state` lines |
| T-RDMA-06 GPU half (production KV bytes) | prepared, not run | `stage3.sh rdma06gpu`, `test_rdma_equals_plain_gets_for_100_keys_stored_by_vllm` |

**Keys.** The keys are the first 100 P-exact keys of
`corpus_llama-3.1-8b-instruct.json`: P-exact-00..15 whole, plus chunk 0 of
P-exact-16. They are derived by production code (`TokenHasher(256)`, then
`IPCCacheServerKey`, then `ipc_key_to_object_keys`), with vLLM's model name
(the Llama-3.1-8B snapshot path), world size 1 and worker 0.

**Payloads.** Every object uses the production Llama-3.1-8B layout: 32 MiB, written as a meta record
`<key>|m` plus 64 segment records `<key>|s|<i>` of 512 KiB. The bytes are synthetic: each object's
32-bit words are distinct across all 100 objects, so a misplaced segment is
detected. They are not real KV values; the GPU half covers that.

**Fetches.** The payloads are stored through LMCache's own Aerospike adapter.
The keys are then read back in 32 fetches of at most 4 chunks (256 slots, one
server command each):
- P-exact-00..14 as whole prompts of 1, 2 or 4 chunks;
- P-exact-15 as 16 fetches of 4 chunks;
- P-exact-16 chunk 0 alone.

Each fetch goes through the production pipelined path into a freshly
registered L1 RDMA window and must complete `PIPELINED`. Every landed object
is compared byte for byte with a plain (non-RDMA) get, and with the stored
payload.

Runs 2 and 3 ran only the 4-chunk test, and their kv-sink logs show no late
completions and no error lines. Runs 1 and 4 also ran the original 64-chunk
test, which hit D-12 exactly as in `item2/`.

**Finding (it shapes T-FLT-07 and T-PIPE-07).** rxe0 sits on `lo` and both
ends use GID 127.0.0.1, so RDMA writes bypass `lo` entirely. One run moved
6.72 GB over `lo`, which is the TCP stores and plain gets, 2 × 3.36 GB
(`lo_counters.txt`). The 3.36 GB of RDMA writes did not appear. So netem and
iptables cannot touch the RDMA path on this box (see `stage5/flt07_rdma_down.sh`).

## Defects found

| Sev | Defect | Evidence |
|---|---|---|
| S3 | `kvsink_smoke.sh` pytest crashed in 2 of 7 warm-ups: once `Fatal Python error: Aborted` in `alloc_pinned_ptr`, once a segfault in logging. Both times it was building the second test's `StorageManager`, right after the first test fell back on a cold server. This suggests a write after free from the abandoned fetch in a torn-down process (the test, not the server's steady state). `kvsink_restart` now retries the warm-up once. | segfault: `logs/smoke_crash/pipelined_it_segv.txt`; the abort's log was overwritten by the next dry run |

## Harness

[HARNESS.md](HARNESS.md) is the runbook. It covers each section's tests, faults
and pass criteria, the fault hooks that exist and are missing, and the CPU
dry-run results. Host changes are in [CHANGES.md](CHANGES.md); versions are in
[VERSIONS.md](VERSIONS.md).
