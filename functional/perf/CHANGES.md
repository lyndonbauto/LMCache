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

## Phase 2: aero-kvsink-bp recreated with the scratch disk

`/dev/vdc1` was mounted at `/mnt/scratch` by the host owner (approved; logged in
`functional/HOST-CHANGES.md`, 9a069f71). This item did not mount, format or add it to
`fstab`. It only created `/mnt/scratch/perf-aero/` and recreated the kv-sink container so
that directory is visible inside (`functional/perf/recreate_kvsink.sh` on the box, run
2026-10-02 20:50Z):

1. `asd` was stopped; `docker inspect` of the old container is saved as
   `/root/lmc-work/functional/perf/aero-kvsink-bp.orig.inspect.json`.
2. The old container was committed to the image `aero-kvsink-bp:perf2-base`
   (`sha256:e274d36add9c…`), because its writable layer holds the packages installed for
   the server build.
3. The old container was stopped and renamed `aero-kvsink-bp-orig` (kept, not removed).
4. A new `aero-kvsink-bp` was created from that image with the same settings (host
   network, `/dev/infiniband/uverbs0`, `memlock` unlimited, `CAP_IPC_LOCK`, seccomp
   unconfined, `/root/lmc-work` bind mount, `sleep infinity`) plus
   `-v /mnt/scratch/perf-aero:/mnt/scratch/perf-aero`
   (`aero-kvsink-bp.perf2.inspect.json`).

`asd` still binds only to 127.0.0.1 (the config's service and fabric addresses). `fio`
was installed in the new container with apt for the disk measurement.

Phase 2 uses the same template and the same generated config path; `@FILE@` is
`/mnt/scratch/perf-aero/lmcache.dat`, `@FILESIZE@` is 2.0x the KV (256G / 512G / 1022G), and
the free-space floor (60 GB) is checked on the data file's filesystem. The last generated
config (128k, `filesize 1022G`) stays at
`/root/lmc-work/functional/perf/aerospike-kvsink-bp-perf.conf`.

State at the end of phase 2: `asd` stopped (nothing on 3700-3703; the new container is up
and idle), `/mnt/scratch/perf-aero/` kept and empty, `/mnt/scratch` still mounted (4823 GB
free), boot disk 169 GB free (1 GB less than after phase 1; not investigated, likely the
committed image layer and the session logs). To go back to the old container: stop and remove `aero-kvsink-bp`,
then rename `aero-kvsink-bp-orig` back and start it.

## Other state

- `lmc-c`: `perftest` (24.10.0, apt) installed for the lw experiments' `ibbw.sh`; no
  perftest process left running. `vllm serve` and `lmcache server` are started and stopped per
  session by `functional/perf/perf_session.sh`, all on 127.0.0.1 (listener check after every
  start).
- `aerospike-ce`, `lmc-b`, `lmc-d`, `lmc-newstack`, `aero-kvsink`, `aero-n1..3`: not touched.
- Charts were rendered on the local machine (matplotlib 3.10.8), not in `lmc-c`.

## lw experiments (LW-EXPERIMENTS.md)

- `aero-kvsink-bp` was used as in phase 2 (data file `/mnt/scratch/perf-aero/lmcache.dat`,
  64 GiB, created and deleted twice); asd stopped at the end, the container idles on `sleep`.
- `/root/lmc-work/LMCache/lmcache/v1/layerwise/pump.py` carried a two-line debug patch
  during the timeline run only (00:00-00:02Z, 3 Oct); reverted with `git checkout --`,
  `git status` clean afterwards. Never committed.
- Box-only runner scripts `lwexp_run.sh` and `lwexp2_run.sh` stay in
  `/root/lmc-work/functional/perf/` (not committed).
