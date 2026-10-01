# Stage 2a host changes

None. Every session ran in the existing `lmc-c` container against the existing
`aerospike-ce` container; the only state changed was the Aerospike set
`lmcache.kv_chunks`, truncated before each session (allowed without asking).

# Stage 2b host changes

None. Sessions ran in `lmc-c` against `aerospike-ce`; state changed only in
the Aerospike set `lmcache.kv_chunks` (truncated before each session, and
T-LKP-03 deleted the 65 records of one chunk). `aerospike-ce-t2` and
`aero-kvsink` were not touched.

# Stage 2c host changes

None. Sessions and reference servers ran in `lmc-c` against `aerospike-ce`;
state changed only in the Aerospike set `lmcache.kv_chunks` (truncated
before each session). Two reference vLLM servers shared the GPU at a time
(ports 8000 and 8001, both on 127.0.0.1). `aero-kvsink`, `lmc-b` and the
CPU worker's cluster were not touched. One more `VLLM::EngineCore` zombie
(pid 605316, from a reference server stopped at 17:54 UTC) joins the four
from Sep 30; none holds VRAM.
