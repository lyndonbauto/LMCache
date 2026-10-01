# Stage 3 prep: the Aerospike kv-sink server build

The Aerospike server that implements `kv-sink-fetch-pipelined` builds and runs
on the test box, in its own container, beside Aerospike CE. LMCache's
pipelined RDMA integration test passes against it over RC on Soft-RoCE
(`rxe0`, GID index 1) once the server's stripes are registered. The first
pipelined fetch after each server start falls back (server issue 5), so
Stage 3 must warm the server before testing.

**Entry-criterion gap.** Section 4 of the functional test plan asks for a
server that sends one write-with-immediate per piece and does not fence. This
build does the first and not the second: it replies only after every write
of the command has completed (server issue 4). Data and outcomes are correct,
and LMCache reports `pipelined`, but nothing overlaps. Functional tests can
run on it; overlap and timing claims cannot.

## What was built

| Item | Value |
| --- | --- |
| Source | `aerospike-server` branch `feat/kv-sink-fetch-pipelined` |
| Commit | `512b0c2079eb41a330888211a26b006eab721323` ("examples: build the kv-sink client against rdma-core 39"), the commit `aerospike_server_issues.md` reviewed |
| Version string | `Aerospike Community Edition build 8.1.3.0-112-g512b0c207` |
| Source on the box | `/root/lmc-work/aerospike-server-kvsink` (private: rsync copy only, no `.git`, never commit or push it) |
| Submodules | All 14 initialized locally and copied with the tree; 6 are private `citrusleaf`/`aerospike` repos the box cannot fetch |
| Binary | `/root/lmc-work/aerospike-server-kvsink/target/Linux-x86_64/bin/asd` (81 MB, links `libibverbs.so.1` and `libefa.so.1`) |
| Container | `aero-kvsink`, image `lmcache-rocm:day1` (Ubuntu 24.04, gcc 13, rdma-core 50), no GPU devices |
| Build time | 93 s wall clock, all modules included, at `nice -n 19` and `-j8` |

### Build steps

The source was copied without git metadata, so the build reads its version
from `/work/VERSION` and `/work/EVENT` (the server's CI path) inside the
container, and a throwaway `git init` keeps `make pcre2lib`'s
`git -C modules/pcre2 submodule update --init` from failing.

```bash
# Local machine: copy the source (no .git pointers, no build outputs).
rsync -a -e "ssh -o ControlPath=/tmp/mi300x.cm" --exclude='.git' --exclude='/target/' \
  --exclude='/examples/kv-sink/kvsink_client' --exclude='/modules/*/build/' \
  --exclude='/modules/*/installation/' --exclude='/modules/*/target/' \
  ~/github/aerospike-server-kvsink/ root@165.245.137.3:/root/lmc-work/aerospike-server-kvsink/

# Box: the container (RDMA flags as lmc-c, without /dev/kfd and /dev/dri).
docker run -d --name aero-kvsink --network host --device /dev/infiniband/uverbs0 \
  --ulimit memlock=-1:-1 --cap-add IPC_LOCK --security-opt seccomp=unconfined \
  -v /root/lmc-work:/root/lmc-work lmcache-rocm:day1 sleep infinity

# In the container: build deps (bin/install-dependencies.sh's list, plus verbs).
apt-get install -y --no-install-recommends lemon re2c libssl-dev zlib1g-dev \
  autoconf automake cmake dpkg-dev fakeroot g++ git libtool make pkg-config \
  libcurl4-openssl-dev libldap2-dev libgtest-dev gcc-13-plugin-dev \
  libibverbs-dev ibverbs-providers ibverbs-utils rdma-core python3 curl

# In the container: build (script: /root/lmc-work/functional/stage3/build_server.sh).
echo 8.1.3.0-112-g512b0c207 > /work/VERSION
echo '{"after":"512b0c2079eb41a330888211a26b006eab721323","ref":"refs/heads/feat/kv-sink-fetch-pipelined"}' > /work/EVENT
cd /root/lmc-work/aerospike-server-kvsink && git init -q && make cleanmodules
nice -n 19 make -j8
```

## Configuration and ports

Config: [`functional/configs/aerospike-kvsink.conf`](../configs/aerospike-kvsink.conf).
Everything binds to `127.0.0.1`, clear of Aerospike CE's 3000 to 3003:

| Port | Use |
| --- | --- |
| 3100 | service (clients, info commands, kv-sink) |
| 3101 | fabric |
| 3102 | heartbeat (mesh) |
| 3103 | admin |

- **Namespace `lmcache`, `storage-engine memory`, `data-size 16G`.** kv-sink
  sends values straight from the memory stripes, so the namespace must be in
  memory. Eight 2 GiB stripes. Data does not survive a server restart; add a
  `file` backing if a test needs that (T-FLT-04).
- **Pinned memory.** Each stripe is registered with `ibv_reg_mr` on its first
  fetch, so a warm server pins all 16 GiB (plus heap, about 18 GB resident).
  `memlock` is unlimited in the container.
- **No kv-sink switch.** The four kv-sink info commands are always on. The
  device is the first verbs device the process sees (`rxe0`, the only one in
  `aero-kvsink`) and the RC GID index is fixed at 1 (issue 6), which is right
  on this box.
- The work directory is `/root/lmc-work/aerospike-kvsink-data/work`. This
  build ignores an `info` stanza ("obsolete") and wants `admin` instead.

## Start, stop, warm up

```bash
# On the host, from the LMCache-cpu clone (or any clone of prototype-stage1).
functional/harness/kvsink_server.sh start    # pid, 127.0.0.1:3100, log in functional/stage3/logs/asd-kvsink.log
functional/harness/kvsink_server.sh status
functional/harness/kvsink_server.sh stop     # SIGTERM, clean shutdown; kill -9 after 30 s
```

The start script raises the open-file limit (`proto-fd-max 15000` fails under
`docker exec`'s default of 1024) and creates the work directory's `smd`.

**Warm up after every start.** Run the smoke once (below), or any pipelined
fetch that touches every stripe, before Stage 3 measures anything. On a cold
server the first fetch registers each stripe inside the fetch (about 0.5 s
each on Soft-RoCE, 3.5 s for seven), past the C client's default 1,000 ms
info timeout, and the retrieve falls back.

LMCache points at it with
`--l2-adapter '{"type":"aerospike","hosts":"127.0.0.1:3100","namespace":"lmcache","rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,...}}'`
(window count and size as the Stage 3 harness sets them).

## Smoke result (2026-10-01)

Script: [`functional/harness/kvsink_smoke.sh`](../harness/kvsink_smoke.sh),
run inside `aero-kvsink`. Output on the box in
`/root/lmc-work/functional/stage3/smoke/`.

| Check | Result |
| --- | --- |
| Info: `build`, `status`, `edition`, `namespaces` | `8.1.3.0-112-g512b0c207`, `ok`, Community Edition, `lmcache` (memory, 16 GiB, 8 stripes) |
| Server's `examples/kv-sink/kvsink_client` (port patched to 3100, built on the box only): register, then `kv-sink-fetch` of one 64 KiB record | Pass. RC queue pair, server GID `::ffff:127.0.0.1`; `results=ok`, `bytes=65536`, payload in the buffer ("PASS: payload arrived by RDMA"). 485 ms round trip, mostly the first stripe's registration |
| LMCache `test_aerospike_pipelined_rdma_integration.py`, cold server | 2 of 3. `test_a_missing_record_falls_back_to_a_whole_reload` and `test_closing_releases_the_servers_region` pass. `test_a_pipelined_fetch_lands_every_stored_byte_in_l1` gets `FELL_BACK`: the server registered seven stripes during that fetch (issue 5) |
| Same test, warm server | **3 of 3.** `kv-sink-register`, then `kv-sink-fetch-pipelined` with one write-with-immediate per slot; every stored byte lands in L1, outcome `PIPELINED` |

The LMCache side ran from the `LMCache-cpu` clone at `a8f06e06` with the
extensions copied from the Day 1 build (no `csrc/` changes since
`a3456e2c`), in `aero-kvsink` with `/work/deps` linked to
`/root/lmc-work/deps` so `libaerospike.so` resolves through the extension's
runpath.

## Known server issues Stage 3 will likely hit

From [`aerospike_server_issues.md`](../../docs/design/v1/distributed/l2_adapters/aerospike_server_issues.md),
which reviewed this exact commit:

| # | Issue | Likely in Stage 3 | Affects |
| --- | --- | --- | --- |
| 4 | Pipelined fetch replies only after every write completes | **Certain.** Every pipelined fetch; no overlap. Large commands may also approach the 1,000 ms info timeout on Soft-RoCE | Plan entry criterion; any timing; possibly T-E2E-05 (P-long) |
| 5 | Stripes registered lazily on the fetch path | **Certain** on the first fetch after each server start (seen in the smoke) | T-E2E-04 "pipelined on every eligible request" unless warmed; any test after a server restart (T-FLT-04) |
| 9 | A dead client's region is never reclaimed (16 regions per node) | **Likely.** The harness kills LMCache with `kill -9` on restarts, so each restart leaks a region; after 16, every register fails and every retrieve loads whole objects | T-E2E-03/04 restarts, T-FLT-05, T-FLT-06. Restart the kv-sink server between test groups |
| 7 | Concurrent fetches on one region corrupt each other | **Likely** under concurrency: expect declined slots, a disabled region or a crash | T-PIPE-08, T-PIPE-10, T-E2E-09 |
| 8 | One failed write disables the region for good | **Likely** after any stalled or failed transfer: later fetches on that registration all fall back until LMCache restarts | T-PIPE-06, T-FLT-07, and anything after issue 7 |
| 10 | Reap loop busy-spins an info thread (up to 30 s) | Likely in stall tests; can starve the node's info threads, including the client's tend | T-PIPE-06, T-FLT-07 |
| 1 | Record released while its bytes are still being sent | Possible but rare: needs a delete or eviction, defrag and a new write into the same block during one transfer | T-EVT-01, T-EVT-04, T-FLT-10 |
| 11 | kv-sink fetches never apply read-touch | Only with a TTL and read-touch configured; this config uses `default-ttl 0` | T-EVT-03, T-STO-07 if run against this server |
| 6 | Device and GID index not configurable | Not on this box (`rxe0` is the only device, GID 1 is right) | None here |
| 2, 3 | EFA write never detected; crash on the register after a refused device | No: EFA only | None here |

Minor items: failed writes log one line each with no region ID (log noise in
fault tests); the SRD retry count does not apply to RC.

## State left

- Container `aero-kvsink` running (`sleep infinity`), server **stopped**,
  nothing listening on 3100 to 3103.
- Source and build: `/root/lmc-work/aerospike-server-kvsink`.
- Data and work directory: `/root/lmc-work/aerospike-kvsink-data/`.
- Logs, smoke output and scripts: `/root/lmc-work/functional/stage3/`.
- Container-local only: `/work/VERSION`, `/work/EVENT`, `/work/deps` link.
