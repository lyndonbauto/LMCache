# Stage 2a host changes

None. Every session ran in the existing `lmc-c` container against the existing
`aerospike-ce` container; the only state changed was the Aerospike set
`lmcache.kv_chunks`, truncated before each session (allowed without asking).

# Stage 2b host changes

None. Sessions ran in `lmc-c` against `aerospike-ce`; state changed only in
the Aerospike set `lmcache.kv_chunks` (truncated before each session, and
T-LKP-03 deleted the 65 records of one chunk). `aerospike-ce-t2` and
`aero-kvsink` were not touched.
