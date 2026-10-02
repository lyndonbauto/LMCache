# Perf sanity check: changes to the box

No host change, no product-code change, and no change to `ufw`, `sshd` or `/dev/vdc`.
Everything below was done inside the rules for Aerospike containers bound to 127.0.0.1.

## aero-kvsink-bp (kv-sink server, 127.0.0.1:3700-3703): reconfigured, not recreated

The container already bind-mounts `/root/lmc-work` (on the boot disk, `/dev/vda1`), so the
data file lives at `/root/lmc-work/perf-aero/lmcache.dat` and no new mount was needed.

| Config | File | Use |
|---|---|---|
| Original (kept unchanged, for restoring) | `functional/configs/aerospike-kvsink-bp.conf` in the box tree (`/root/lmc-work/LMCache/...`) | `storage-engine memory`, `data-size 16G`; what every earlier stage used |
| Perf template | `functional/configs/aerospike-kvsink-bp-perf.conf.in` | `perf.sh` fills in `@FILE@` and `@FILESIZE@` |
| Perf config (last generated) | `/root/lmc-work/functional/perf/aerospike-kvsink-bp-perf.conf` (box only) | The device namespace below; regenerated for each length |

The `lmcache` namespace stanza used for the cached runs (`filesize` per length: 45G for 8k,
90G for 16k, 8G for the smoke; that is 32 prompts x KV size x 1.4):

```text
namespace lmcache {
    replication-factor 1
    nsup-period 120
    default-ttl 0
    max-record-size 1048576
    stop-writes-sys-memory-pct 100
    storage-engine device {
        file /root/lmc-work/perf-aero/lmcache.dat
        filesize 45G
        direct-files true
        read-page-cache false
        post-write-cache 0
        max-write-cache 8G
    }
}
```

- `direct-files true`: the file is opened with `O_DIRECT`, so reads bypass the page cache.
- `read-page-cache false` (the default) and `post-write-cache 0`: no read cache, and no
  recently written blocks are kept in memory (`post_write_q_limit = 0`), so every read after
  the store's write buffers are flushed comes from the disk. The server confirms the values
  at start (`get-config`, logged in `progress.log`).
- `max-write-cache 8G` (default 64M): buffers the store pass rather than failing writes with
  `DEVICE_OVERLOAD` (D-26). It holds only writes not yet flushed.
- The primary index stays in RAM, which is Aerospike's normal design for a device namespace
  (64 bytes per record; 133,120 records at 16k is about 8 MiB).
- The `lmcache_evict` test namespace of the original config is left out.

Evidence that reads came from disk: `asd`'s `/proc/<pid>/io` `read_bytes` (storage reads
that bypassed the page cache) is recorded around every session (`<session>/asd_read_bytes.txt`
and `progress.log`).

Lifecycle, per length (`perf.sh cached:<len>`, and again for `perf.sh lwwait:<len>`): stop
`asd`, delete any old data file, write the perf config, start `asd` (empty sparse file),
store, measure, stop `asd`, delete the data file. A length is refused if its data file would
leave under 60 GB free on `/`. Lowest free space seen: 101 GB (16k file in place).

State at the end of phase 1: `asd` in `aero-kvsink-bp` is stopped (the container itself is
up, idle, nothing listening on 3700-3703); `/root/lmc-work/perf-aero/` is empty; the last
generated perf config (16k, `filesize 90G`) is kept at
`/root/lmc-work/functional/perf/aerospike-kvsink-bp-perf.conf`, and the original memory
config is unchanged at `functional/configs/aerospike-kvsink-bp.conf`. To go back to the
original: start `asd` with the original config as every earlier stage did. Free space on `/`
is back to 170 GB.

## Other state

- `lmc-c`: nothing installed; `vllm serve` and `lmcache server` are started and stopped per
  session by `functional/perf/perf_session.sh`, all on 127.0.0.1 (listener check after every
  start).
- `aerospike-ce`, `lmc-b`, `lmc-d`, `lmc-newstack`, `aero-kvsink`, `aero-n1..3`: not touched.
- Charts were rendered on the local machine (matplotlib 3.10.8), not in `lmc-c`.
