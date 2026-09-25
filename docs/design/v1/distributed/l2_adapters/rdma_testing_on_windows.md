# Running the RDMA tests from a Windows workstation

How to stand up an Ubuntu VM under Hyper-V on Windows Pro and run the Aerospike
RDMA and layerwise test suites in it. The protocol and design rationale are in
[`aerospike_rdma.md`](aerospike_rdma.md); this document is only the
environment.

**What this environment proves:** Track A criteria A1--A6 and A9 from
[`track-a-acceptance.md`](../../layerwise/track-a-acceptance.md) --- the
device-free logic harnesses, the Soft-RoCE fabric harnesses against the mock
`kv-sink` server, and the layerwise contract suite.

**What it cannot prove:** A7 needs real EFA hardware, and A8 needs an Aerospike
server built from branch `sriram/kv-rdma-poc`. Neither is reachable from a
Windows workstation. See [What this VM cannot cover](#what-this-vm-cannot-cover).

## Why a VM and not WSL2 or Docker Desktop

The fabric tests need Soft-RoCE, which is the **kernel module** `rdma_rxe`. As
[`aerospike_rdma.md`](aerospike_rdma.md#reproducing-the-soft-roce-test-setup)
notes, loading it inside a privileged container loads it *for the host kernel* ---
so containers do not sidestep the requirement, they inherit it. On Windows the
kernel under both WSL2 and Docker Desktop is Microsoft's, which does not ship
`rdma_rxe`, so `modprobe` fails and there is nothing to attach `rxe0` to.

A VM running a stock Ubuntu kernel has the module. WSL2 is still fine for the
device-free tiers (steps 6.1 and 6.3 below) if you prefer it for those.

## 1. Enable Hyper-V

This guide assumes Windows Pro, which ships Hyper-V. Enable it once, from an
elevated PowerShell, then reboot:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
```

## 2. Create the VM

Some Windows builds ship Hyper-V Manager without Quick Create (`vmcreate.exe`).
Creating the VM from PowerShell works either way and is scriptable. From an
**elevated** PowerShell, with an Ubuntu Server ISO already downloaded:

```powershell
$vm  = "lmcache-rdma"
$iso = "$env:USERPROFILE\Downloads\ubuntu-24.04.5-live-server-amd64.iso"
$vhd = "$env:PUBLIC\Documents\Hyper-V\Virtual hard disks\$vm.vhdx"

New-VM -Name $vm -Generation 2 -MemoryStartupBytes 8GB `
       -NewVHDPath $vhd -NewVHDSizeBytes 80GB -SwitchName "Default Switch"
Set-VMProcessor -VMName $vm -Count 8
Add-VMDvdDrive  -VMName $vm -Path $iso
Set-VMFirmware  -VMName $vm -SecureBootTemplate MicrosoftUEFICertificateAuthority `
                -FirstBootDevice (Get-VMDvdDrive -VMName $vm)
Start-VM $vm
vmconnect.exe localhost $vm
```

`-SecureBootTemplate MicrosoftUEFICertificateAuthority` is required: with the
default Windows template a Generation 2 Linux guest will not boot the ISO.

| Resource | Minimum | Comfortable |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 8 GB | 16 GB |
| Disk | 40 GB | 80 GB |

The VHDX is dynamically expanding, so 80 GB costs only what the guest actually
writes --- around 15 GB once torch and the build artifacts are in place.

**Size RAM for pytest, not for RDMA.** The harness slabs are tiny (256 KiB and
512 KiB), but `tests/conftest.py` creates a **5 GB** `MixedMemoryAllocator` for
the whole session and applies it to every test under `tests/`. Anything below
about 6 GB of usable memory fails at fixture setup with
`DefaultCPUAllocator: can't allocate memory`. If the host cannot spare that,
see [the swap workaround](#if-the-host-cannot-spare-the-ram).

If Hyper-V refuses to start the VM with `Insufficient system resources`, the
host does not have that much *available* physical RAM --- dynamic memory is off
by default here, so the full startup amount must be backed up front. Either free
host memory, or enable dynamic memory and accept the swap workaround:

```powershell
Set-VMMemory -VMName $vm -DynamicMemoryEnabled $true `
             -MinimumBytes 2GB -StartupBytes 4GB -MaximumBytes 12GB
```

During the installer, choose the full **Ubuntu Server** install rather than the
minimized one, tick **Install OpenSSH server**, and on the storage screen edit
`ubuntu-lv` to use the whole volume group --- the guided LVM default leaves about
half the disk unallocated. Eject the ISO afterwards with
`Set-VMDvdDrive -VMName $vm -Path $null`.

No GPU passthrough is needed, and per A9 no Track A test may require one.

## 3. Get the code into the VM

Clone inside the VM rather than mounting the Windows checkout. A shared folder
gives you CRLF line endings on shell scripts, slow `make` dependency checks, and
a `build/` directory fought over by two operating systems.

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <your-fork-or-remote> ~/LMCache
cd ~/LMCache
```

To keep editing in Cursor on Windows, enable SSH in the VM
(`sudo apt-get install -y openssh-server`) and connect over Remote-SSH, which
runs the tests on the Linux side while the editor stays on Windows.

The Default Switch hands out NAT addresses that change across reboots, so pin a
name instead of an address. Install `avahi-daemon` in the guest and reach it at
`<hostname>.local`, then give it an alias in `%USERPROFILE%\.ssh\config`:

```text
Host lmcache-vm
    HostName ubuntu.local
    User vk
    IdentityFile ~/.ssh/lmcache-rdma-vm
```

## 4. Toolchain and Python environment

The C++ harnesses need only `make`, `g++`, and (for the fabric tier)
`libibverbs`. The Python tiers need LMCache importable.

```bash
# Build tooling and RDMA userspace.
sudo apt-get install -y build-essential python3.12-dev ninja-build \
    rdma-core libibverbs-dev ibverbs-utils ibverbs-providers perftest

# Python environment.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv --python 3.12
source .venv/bin/activate
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
uv pip install numpy "pybind11>=2.12"
MAX_JOBS=4 NO_GPU_EXT=1 uv pip install -e . --no-build-isolation
uv pip install -r requirements/test.txt
```

`NO_GPU_EXT=1` builds the common C++ extensions without a GPU backend. A CPU
torch wheel is enough for everything in this document. `MAX_JOBS=4` keeps
parallel `g++` from exhausting a small guest.

Three of those are easy to miss and each fails confusingly:

- **`python3.12-dev`.** Without it the extension build dies on
  `fatal error: Python.h: No such file or directory`, and because `pip install
  -e .` still leaves the source tree importable, `import lmcache` appears to
  work from the repo root while every native path is missing. Confirm with
  `ls lmcache/*.so`, which should list `lmcache_native`, `lmcache_fs`, and
  `lmcache_redis`.
- **`numpy`.** `tests/conftest.py` imports it at collection time, so without it
  every test errors before it runs.
- **`export PATH`.** The `uv` installer puts the binary in `~/.local/bin`, which
  non-interactive SSH sessions do not pick up, so scripted runs fail with
  `uv: command not found` even though it works in a login shell.

The Aerospike native connector (`lmcache_aerospike`) is a further opt-in. You
need it for the adapter itself, the pybind entry points, and
`tests/v1/distributed/test_pipelined_layer_readiness.py`. The harnesses don't
need it. It links against the Aerospike C client. These steps unpack the same
prebuilt 7.3.0 packages `.github/workflows/aerospike_integration.yml` uses into
`.deps/`, which is git-ignored, so nothing is installed system-wide:

```bash
sudo apt-get install -y libssl-dev libuv1-dev libyaml-dev

VERSION=7.3.0 UBUNTU=ubuntu22.04
BASE="aerospike-client-c-libuv_${VERSION}_${UBUNTU}_x86_64"
DEPS="$HOME/LMCache/.deps" INSTALL="$HOME/LMCache/.deps/aerospike-install"
mkdir -p "$DEPS" "$INSTALL"
curl -fsSL -o "$DEPS/$BASE.tgz" \
  "https://download.aerospike.com/artifacts/aerospike-client-c/$VERSION/$BASE.tgz"
tar xzf "$DEPS/$BASE.tgz" -C "$DEPS"
dpkg-deb -x "$DEPS/$BASE/aerospike-client-c-libuv-devel_${VERSION}-${UBUNTU}_amd64.deb" "$INSTALL"
dpkg-deb -x "$DEPS/$BASE/aerospike-client-c-libuv_${VERSION}-${UBUNTU}_amd64.deb" "$INSTALL"

cat > "$DEPS/aerospike-client-c.env" <<EOF
export AEROSPIKE_INCLUDE_DIR=$INSTALL/usr/include
export AEROSPIKE_LIBRARY_DIR=$INSTALL/usr/lib
export LD_LIBRARY_PATH=$INSTALL/usr/lib:\${LD_LIBRARY_PATH:-}
EOF
source "$DEPS/aerospike-client-c.env"

MAX_JOBS=2 NO_GPU_EXT=1 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 \
  uv pip install -e . --no-build-isolation
```

The Ubuntu 22.04 client packages work unchanged on a 24.04 guest. CI uses the
legacy spelling `BUILD_AEROSPIKE=1`, which also works. Source the `.env` file in
every new shell before building or running tests. The build needs
`AEROSPIKE_INCLUDE_DIR` to find the headers, and the import needs
`LD_LIBRARY_PATH` to find `libaerospike.so`.

Confirm the RDMA path was compiled in:

```bash
python -c "from lmcache.lmcache_aerospike import LMCacheAerospikeClient as C; \
print(hasattr(C, 'issue_pipelined_fetch_by_slots'))"   # expect True
```

## 5. Create the Soft-RoCE device

```bash
sudo modprobe rdma_rxe
sudo rdma link add rxe0 type rxe netdev lo
ibv_devinfo -d rxe0     # expect PORT_ACTIVE
rdma link show
```

Ubuntu 24.04's stock `generic` kernel ships `rdma_rxe`, so this usually works
unmodified. If `modprobe` reports the module is missing, install the extra
modules package for the running kernel and retry:

```bash
sudo apt-get install -y linux-modules-extra-$(uname -r)
```

**Pick the GID index deliberately.** On `lo` it is **1**, not 0, because `lo`'s
all-zero MAC makes GID 0 an unroutable `fe80::` address and the queue pair then
fails at RTR with `ENETUNREACH`. Confirm and export:

```bash
cat /sys/class/infiniband/rxe0/ports/1/gids/*
export RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
```

**Check the locked-memory limit.** `ibv_reg_mr` fails if the pinned slab exceeds
`RLIMIT_MEMLOCK`:

```bash
ulimit -l
```

Ubuntu's default is around 500 MB, comfortably above the harnesses' 256 KiB and
512 KiB slabs. If yours is smaller, add to `/etc/security/limits.conf` and log
in again:

```text
*    soft    memlock    unlimited
*    hard    memlock    unlimited
```

Prove the fabric works before blaming LMCache:

```bash
ibv_rc_pingpong -d rxe0 -g 1 &
ibv_rc_pingpong -d rxe0 -g 1 localhost
```

The device does not survive a reboot. Re-run the two commands at the top of this
step, or install them as a systemd unit:

```ini
# /etc/systemd/system/soft-roce.service
[Unit]
Description=Soft-RoCE rxe0 on lo
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/modprobe rdma_rxe
ExecStart=/usr/bin/rdma link add rxe0 type rxe netdev lo

[Install]
WantedBy=multi-user.target
```

Note `/usr/bin/rdma`, not `/usr/sbin/rdma`: systemd needs an absolute path and
rejects a wrong one at boot with the easily-missed `status=203/EXEC`, leaving
the module loaded but no device. Verify after `systemctl enable --now
soft-roce.service` with `rdma link show`, and remember that empty output there
is exactly what a silently failed unit looks like.

### If the host cannot spare the RAM

The `make` targets in step 6.1 and 6.2 bypass pytest entirely, so they run
happily in under 2 GB. Only the pytest tiers need the 5 GB session allocator. On
a host too busy to back a large guest, grow the guest's swap instead of fighting
for physical memory:

```bash
sudo swapoff /swap.img
sudo fallocate -l 12G /swap.img
sudo chmod 600 /swap.img
sudo mkswap -q /swap.img
sudo swapon /swap.img
sudo sysctl -w vm.overcommit_memory=1
```

The allocator reserves 5 GB but does not touch most of it, so the pages stay
unbacked and the suites run at normal speed. Verified working in a guest with
1.8 GB of RAM. Make it permanent with `vm.overcommit_memory=1` in
`/etc/sysctl.d/`.

## 6. Run the tests

`tests/v1/distributed/` is excluded from the standard suite in
[AGENTS.md](../../../../../AGENTS.md), so every tier below has to be invoked
explicitly.

### 6.1 Device-free C++ harnesses

Sharding, slot planning, the wire codec, the pipelined-fetch session, and the
notification-depth clamp. These link neither `libibverbs` nor the Aerospike
client, so they run before you have a device --- and in WSL2.

```bash
make -C tests/v1/distributed/rdma logic-test
# or through pytest, which builds them for you:
pytest -xvs tests/v1/distributed/rdma/test_shard_plan.py \
            tests/v1/distributed/rdma/test_slot_planner.py \
            tests/v1/distributed/rdma/test_request_plan.py \
            tests/v1/distributed/rdma/test_pipelined_fetch.py \
            tests/v1/distributed/rdma/test_pipelined_fetch_session.py \
            tests/v1/distributed/rdma/test_pipelined_fetch_issue.py \
            tests/v1/distributed/rdma/test_notification_depth.py
```

### 6.2 Fabric harnesses (needs step 5)

Byte-equivalence and layer pipelining, driven through the production
`RdmaContext` and codec against `KvSinkMockWriter`.

```bash
make -C tests/v1/distributed/rdma test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
# or:
RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 pytest -xvs \
    tests/v1/distributed/rdma/test_rdma_equivalence.py \
    tests/v1/distributed/rdma/test_rdma_pipeline.py
```

These **skip** rather than fail without a device: the binaries exit 77, the
automake "skip" convention, and the pytest wrapper turns that into a skip. A
skipped run means step 5 did not take effect.

### 6.3 Python: contract and configuration

```bash
pytest -xvs tests/v1/layerwise/
pytest -xvs tests/v1/distributed/l2_adapters/test_rdma_registration.py
pytest -xvs tests/v1/distributed/test_aerospike_l2_adapter_config.py
```

`tests/v1/layerwise/` includes the conformance suite and
`AerospikeLayerArrivalSource`'s own tests, which run against a fake native
client. Once `lmcache_aerospike` is built with RDMA (step 4), also run the
readiness tests that go through the real extension:

```bash
source .deps/aerospike-client-c.env
pytest -xvs tests/v1/distributed/test_pipelined_layer_readiness.py
```

If you run all of `tests/v1/distributed/rdma/` through pytest, export
`RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1` first. Otherwise the two fabric tests fail
with `ENETUNREACH` rather than skipping, because the device exists but GID 0 on
`lo` is unroutable.

### 6.4 Aerospike CE integration (non-RDMA)

This covers the existing native L2 path, **not** the `kv-sink` RDMA protocol.
Install Docker in the VM and follow the container and environment steps in
`.github/workflows/aerospike_integration.yml`, then:

```bash
export RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \
       AEROSPIKE_TEST_PORT=<mapped port> AEROSPIKE_TEST_NAMESPACE=lmcache
pytest -xvs tests/v1/distributed/test_aerospike_l2_integration.py
```

### 6.5 Lint

```bash
SKIP=rust-fmt,rust-clippy pre-commit run --all-files
```

Drop the `SKIP` once `cargo`, `rustfmt`, and `cargo clippy` are installed;
without them the Rust hooks fail even on Python-only changes.

## What this VM cannot cover

**A7 --- the EFA receive-WR question.** Soft-RoCE is RC-only and ordered, so the
out-of-order and unsolicited-receive behaviour the design guards against cannot
be reproduced here. Answering it needs an EFA-enabled EC2 instance; note the GID
index flips back to 0 there.

**A8 --- a real Aerospike server.** The stock `aerospike/aerospike-server` image
used in step 6.4 does not speak `kv-sink-register` or `kv-sink-fetch`. That
lives on branch `sriram/kv-rdma-poc`. Until it is deployed, every fabric result
above is your side of the contract validated against a mock of the protocol, not
against the server's implementation of it.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `modprobe: FATAL: Module rdma_rxe not found` | Kernel lacks the module. Install `linux-modules-extra-$(uname -r)`; if you are in WSL2, use the VM instead. |
| `DefaultCPUAllocator: can't allocate memory: you tried to allocate 5368713216 bytes` | The 5 GB session allocator in `tests/conftest.py`. Give the guest more RAM or use [the swap workaround](#if-the-host-cannot-spare-the-ram). |
| `fatal error: Python.h: No such file or directory` during install | Missing `python3.12-dev`. Install it and rerun the editable install. |
| `ModuleNotFoundError: No module named 'numpy'` at collection | `numpy` is a test-collection dependency; `uv pip install numpy`. |
| `uv: command not found` over SSH | `~/.local/bin` is not on a non-interactive `PATH`. Export it in the command. |
| `Start-VM`: `Insufficient system resources` | Host lacks that much *available* physical RAM with dynamic memory off. Free host memory or enable dynamic memory. |
| `ibv_modify_qp` fails `ENETUNREACH` at RTR | Wrong GID index. On `lo` use `RDMA_GID_INDEX=1`. |
| Fabric test exits 77 / pytest skips it | No RDMA device visible. Re-run step 5; `rxe0` does not survive a reboot. |
| `rdma link show` empty after a reboot | The boot unit failed. Check `systemctl status soft-roce.service`; `status=203/EXEC` means a wrong `ExecStart` path. |
| SSH stops working after a host reboot | The Default Switch reassigned the guest's NAT address. Use the `.local` name, or find it with `Get-NetNeighbor` filtered to the Hyper-V MAC prefix `00-15-5D`. |
| `ibv_reg_mr` fails | `RLIMIT_MEMLOCK` too low. Check `ulimit -l` and raise `memlock` in `/etc/security/limits.conf`. |
| pytest skips with "libibverbs development headers not found" | Install `libibverbs-dev`, or point `RDMA_CORE_INCLUDE_DIR` and `RDMA_CORE_LIBRARY_DIR` at an out-of-tree rdma-core. |
| pytest skips with "make is not available" or "no C++ compiler" | `sudo apt-get install -y build-essential`. |
| `ld: cannot find -lssl` / `-lcrypto` building `lmcache_aerospike` | Every object compiled; only the link failed. `sudo apt-get install -y libssl-dev` and rebuild. |
| Build succeeds but `lmcache.lmcache_aerospike` does not exist | Neither `BUILD_WITH_AEROSPIKE=1` nor `AEROSPIKE_INCLUDE_DIR` was set in that shell. `source .deps/aerospike-client-c.env` and rebuild. |
| `ImportError: libaerospike.so` at import | `LD_LIBRARY_PATH` not set in this shell. `source .deps/aerospike-client-c.env`. |
| `RuntimeError` naming a rebuild flag from the adapter | The package was built without RDMA. Rebuild with `BUILD_WITH_AEROSPIKE_RDMA=1`. |

## Related

- [`aerospike_rdma.md`](aerospike_rdma.md) --- the wire protocol, the
  registration model, and the canonical Soft-RoCE setup notes.
- [`track-a-acceptance.md`](../../layerwise/track-a-acceptance.md) --- what these
  test tiers are evidence for.
