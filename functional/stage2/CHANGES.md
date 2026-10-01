# Stage 2a host changes

None. Every session ran in the existing `lmc-c` container against the existing
`aerospike-ce` container; the only state changed was the Aerospike set
`lmcache.kv_chunks`, truncated before each session (allowed without asking).
