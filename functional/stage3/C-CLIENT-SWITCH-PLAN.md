# Stage 3 prep: Aerospike C client switch plan

**Answer: LMCache does not need Sriram's modified C client for any kv-sink
command it sends today.** Every kv-sink command (`kv-sink-register`,
`kv-sink-fetch-pipelined`, `kv-sink-deregister`) goes out as an info command
through the stock client's public API. Stage 3 can run on the stock client
7.3.0 already on the box. Don't switch until someone asks for it in Slack;
this page is the plan for when they do.

## How LMCache talks to kv-sink

| Command | Where | Client call |
| --- | --- | --- |
| `kv-sink-register` (once per node, fanned out) | `csrc/storage_backends/aerospike/kv_sink_fanout.cpp` | `aerospike_info_foreach`, then `aerospike_info_node` per node |
| `kv-sink-fetch-pipelined` (per node, per retrieve) | `connector.cpp`, `send_pipelined_info_command` | `aerospike_info_node` on the node that masters the record's partition (`as_partition_get_node`), default info policy (1,000 ms timeout) |
| `kv-sink-deregister` | `kv_sink_fanout.cpp` | `aerospike_info_node` / `aerospike_info_foreach` |
| Plain put, get, exists, batch exists | `connector.cpp` | `aerospike_key_*`, `aerospike_batch_*` |

The command strings are built and parsed in `kv_sink_client.cpp`. The RDMA
side (queue pair, memory registration, receives for the immediates) is
LMCache's own verbs code (`rdma_context.cpp`, `pipelined_fetch_session.cpp`);
the C client only carries text.

## What the modified client adds

`~/github/aerospike-client-c-kvsink` (private), branch
`origin/sriram/kv-sink-batch-prio`, six commits on top of `master`
(`099ebc1a`), 4,791 lines:

- `as_sink` (`as_sink.h`, `as_sink.c`, `as_sink_verbs.c`): a client-side
  sink transport (SRD, RC, or `local` via `process_vm_writev`) that registers
  client memory with each node through `kv-sink-register`, refreshes it with
  a new `kv-sink-touch` command, and expires idle sinks.
- Reads that place the value in registered client memory:
  `as_batch_read_record.sink` and `aerospike_key_get_into()`, carried as a new
  message field `AS_FIELD_SINK = 46` on batch and single-record reads.

It is a different protocol from the info commands LMCache uses. Neither this
server branch (`feat/kv-sink-fetch-pipelined` at `512b0c207`) nor
`sriram/kv-rdma-poc` handles field 46 or `kv-sink-touch` (no sink code under
`as/src/transaction/`), so switching clients today changes nothing on the
wire; it only matters once LMCache adopts the batch-read sink API and a
server that implements it exists.

## How LMCache's build picks the client

`setup_extensions/storage_backend_profiles/aerospike.py`, enabled by
`BUILD_WITH_AEROSPIKE=1`:

| Variable | Use | Box today |
| --- | --- | --- |
| `AEROSPIKE_INCLUDE_DIR` | Header path(s), `;`-separated | `/work/deps/aerospike-install/usr/include` |
| `AEROSPIKE_LIBRARY_DIR` | Library path(s); also baked in as the extension's RUNPATH | `/work/deps/aerospike-install/usr/lib` |
| `AEROSPIKE_EVENT_LIB` | `libuv` (default) adds `-luv` | `libuv` |
| `BUILD_WITH_AEROSPIKE_RDMA=1` | Adds the RDMA sources and `-libverbs` | set |
| `BUILD_WITH_AEROSPIKE_EFA=1` | Adds `-lefa` and the SRD path | not set |

The extension links `-laerospike` from the first library directory that has
it, and finds it at run time through the RUNPATH, so the client in use is
whatever `libaerospike.so` sits in `AEROSPIKE_LIBRARY_DIR` at build time.
Today that is the stock `aerospike-client-c-libuv 7.3.0` for Ubuntu 24.04,
extracted from the release `.deb`s into `/root/lmc-work/deps/aerospike-install`
(`/work/deps/...` inside `lmc-c`).

## Switch plan (when asked)

1. **Copy the source** (rsync only; never push it or put it in the LMCache
   repo). Its submodules (`modules/common`, `modules/lua`, `modules/mod-lua`)
   are not initialized locally but are public, so the box can fetch them:

   ```bash
   cd ~/github/aerospike-client-c-kvsink && git fetch origin && git checkout --detach origin/sriram/kv-sink-batch-prio
   rsync -a -e "ssh -o ControlPath=/tmp/mi300x.cm" ~/github/aerospike-client-c-kvsink/ \
     root@165.245.137.3:/root/lmc-work/aerospike-client-c-kvsink/
   ```

   Copy `.git` too (it is a normal clone, not a worktree), so the box can run
   `git submodule update --init` and the commit can be recorded.

2. **Build in a container**, not in `lmc-c`'s install (for example in
   `aero-kvsink`, which already has the toolchain and rdma-core 50):

   ```bash
   cd /root/lmc-work/aerospike-client-c-kvsink
   git submodule update --init
   apt-get install -y --no-install-recommends libyaml-dev libuv1-dev libssl-dev zlib1g-dev
   nice -n 19 make -j8 EVENT_LIB=libuv
   mkdir -p /root/lmc-work/deps/aerospike-kvsink-install/usr/{lib,include}
   cp target/Linux-x86_64/lib/libaerospike.{so,a} /root/lmc-work/deps/aerospike-kvsink-install/usr/lib/
   cp -r target/Linux-x86_64/include/* /root/lmc-work/deps/aerospike-kvsink-install/usr/include/
   ```

   Keep the stock install in place, so switching back is one variable.

3. **Mind the verbs link.** When `infiniband/efadv.h` is present (it is, from
   `libibverbs-dev`), the client compiles its verbs sink (`-DAS_SINK_VERBS`)
   and expects the application to link `-libverbs -lefa`. LMCache's build adds
   `-lefa` only under `BUILD_WITH_AEROSPIKE_EFA=1`, so either build LMCache
   with `BUILD_WITH_AEROSPIKE_EFA=1` too (compiles the SRD path; harmless on
   Soft-RoCE), or check `ldd`/`nm -u` on the new `libaerospike.so` and expect
   `undefined symbol: efadv_*` at import otherwise.

4. **Rebuild LMCache** in the working tree that will run Stage 3, with the
   Day 1 flags plus the new paths:

   ```bash
   export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8
   export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 BUILD_WITH_AEROSPIKE_EFA=1
   export AEROSPIKE_INCLUDE_DIR=/work/deps/aerospike-kvsink-install/usr/include
   export AEROSPIKE_LIBRARY_DIR=/work/deps/aerospike-kvsink-install/usr/lib
   nice -n 19 pip install -e . --no-build-isolation
   ldd lmcache/lmcache_aerospike*.so | grep -E 'aerospike|verbs|efa'   # must name the kvsink install
   readelf -d lmcache/lmcache_aerospike*.so | grep RUNPATH
   ```

5. **Rerun after the switch**, in this order: the T-CFG-01/02 import checks;
   the Aerospike integration suites against CE (`test_aerospike_l2_integration.py`,
   `test_aerospike_record_layouts_integration.py`; T-STO-01, T-STO-06,
   T-LKP-01, T-LKP-02); the RDMA suite on `rxe0` (T-RDMA-01 to 04);
   `test_aerospike_pipelined_rdma_integration.py` against the kv-sink server
   on 3100 (warm it first); then one end-to-end pass (T-E2E-02/03 on P-exact)
   before continuing Stage 3. Record the client commit and the build in the
   stage's `VERSIONS.md`.

**Back out:** rebuild with `AEROSPIKE_INCLUDE_DIR`/`AEROSPIKE_LIBRARY_DIR`
pointing at `/work/deps/aerospike-install/usr/...` again.
