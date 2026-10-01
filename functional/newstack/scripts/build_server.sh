#!/bin/bash
# Runs inside aero-kvsink-bp. Builds aerospike-server sriram/kv-sink-batch-prio
# @ 046e8558d from an rsync copy without .git (version from /work/VERSION).
set -u
SRC=/root/lmc-work/aerospike-server-kvsink-bp
if ! dpkg -s libibverbs-dev lemon re2c >/dev/null 2>&1; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    lemon re2c libssl-dev zlib1g-dev autoconf automake cmake dpkg-dev fakeroot g++ git libtool \
    make pkg-config libcurl4-openssl-dev libldap2-dev libgtest-dev gcc-13-plugin-dev \
    libibverbs-dev ibverbs-providers ibverbs-utils rdma-core python3 curl
fi
mkdir -p /work
echo "8.1.3.0-111-g046e8558d" > /work/VERSION
echo '{"after":"046e8558d1732ea324308580438091f83c850fe7","ref":"refs/heads/sriram/kv-sink-batch-prio"}' > /work/EVENT
chown -R root:root $SRC
cd $SRC
[ -d .git ] || git init -q
echo "[$(date -u +%T)] cleanmodules"
make cleanmodules > /dev/null 2>&1; echo "cleanmodules exit=$?"
echo "[$(date -u +%T)] build start"
START=$(date +%s)
nice -n 19 make -j8
RC=$?
echo "[$(date -u +%T)] build exit=$RC elapsed=$(( $(date +%s) - START ))s"
ls -la target/Linux-x86_64/bin/ 2>&1
ldd target/Linux-x86_64/bin/asd | grep -E 'verbs|efa'
echo BUILD_DONE rc=$RC
