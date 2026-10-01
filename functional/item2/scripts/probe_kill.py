# Probe: how long one 8 MiB store takes, and where a kill lands.
import sys, time, aerospike, subprocess, random, signal, os, pathlib
sys.path.insert(0, "/work/LMCache-cpu")
from tests.v1.distributed import test_aerospike_storage_integrity_integration as t
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import object_key_to_string
c = aerospike.client({"hosts": [("127.0.0.1", 3200)]}).connect()
for attempt in range(3):
    s = f"probe_{os.getpid()}_{attempt}"
    prog = pathlib.Path(f"/tmp/{s}.txt")
    w = subprocess.Popen([sys.executable, "-c", t._WRITER, "127.0.0.1:3200", "lmcache", s, t.MODEL, str(t.KILL_OBJECT_BYTES), str(prog)])
    while "ready" not in (prog.read_text() if prog.exists() else ""):
        time.sleep(0.05)
    t0 = time.monotonic(); time.sleep(random.uniform(0.3, 1.5)); w.send_signal(signal.SIGKILL); w.wait()
    lines = prog.read_text().split()
    stored = [int(v) for k, v in zip(lines, lines[1:]) if k == "stored"]
    print("attempt", attempt, "elapsed", round(time.monotonic()-t0, 2), "stored", len(stored))
    for i in range((max(stored) if stored else -1) - 1, (max(stored) if stored else -1) + 6):
        k = object_key_to_string(t._key(i))
        st = []
        for suf in ["|m"] + [f"|s|{j}" for j in range(9)]:
            try: st.append(int(c.exists(("lmcache", s, k + suf))[1] is not None))
            except aerospike.exception.RecordNotFound: st.append(0)
        print("  obj", i, "meta,segs =", st)
