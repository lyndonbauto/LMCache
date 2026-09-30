Aerospike
=========

An L2 adapter backed by the native C++ Aerospike connector (the same
``ConnectorBase`` worker-pool harness used by ``fs_native``), wrapped with
``NativeConnectorL2Adapter``.  KV objects are stored under a meta record plus
optional payload segments so values larger than the server record cap are
transparently sharded.

**Prerequisites -- Building with Aerospike support:**

The Aerospike extension is **not** built by default.  Install the Aerospike C
client, then build with ``BUILD_AEROSPIKE=1`` (or set ``AEROSPIKE_INCLUDE_DIR``):

.. code-block:: bash

    BUILD_AEROSPIKE=1 pip install -e .

See :doc:`/developer_guide/extending_lmcache/native_connectors` (section
"Built-in Aerospike backend") for installing the C client into ``.deps/`` and
the ``aerospike-client-c.env`` example.

**Required fields:**

- ``hosts``: Seed hosts as ``host:port[,host:port...]``.

**Optional fields:**

- ``namespace`` (str, default ``"lmcache"``): Aerospike namespace.  Must exist
  on the server and have ``nsup-period > 0`` if you rely on TTL expiry.
- ``set_name`` / ``set`` (str, default ``"kv_chunks"``): Aerospike set name.
- ``num_workers`` (int, default ``8``, > 0): C++ worker threads for I/O.  This
  is the real I/O queue depth -- raise it to push throughput.
- ``read_timeout_ms`` (int, default ``1000``): Client read timeout.
- ``write_timeout_ms`` (int, default ``2000``): Client write timeout.
- ``default_ttl_seconds`` (int, default ``86400``): Record TTL.  ``0`` uses the
  namespace default TTL.
- ``target_segment_bytes`` (int, default ``0``): Target shard size.  ``0`` uses
  the discovered server record cap.
- ``max_record_bytes`` (int, default ``0``): Override the server record cap.
  ``0`` discovers it at construction time.
- ``username`` / ``password`` (str, default ``""``): Optional Enterprise
  Edition authentication.
- ``max_capacity_gb`` (float, default ``0``): Maximum L2 capacity in GB for
  client-side usage tracking / eviction.  ``0`` disables tracking.

**Keeping frequently used entries: read-touch TTL.**  A record's TTL is set
when it is written, so by default an entry expires ``default_ttl_seconds``
after its store however often it is read.  To make expiry follow use instead,
set ``default-read-touch-ttl-pct`` (Aerospike server 7.1 or later) on the
namespace.  A read in the last ``pct`` percent of a record's life then resets
its TTL to the full write TTL:

.. code-block:: text

    namespace lmcache {
        default-ttl 1d
        default-read-touch-ttl-pct 80   # a read after 20% of the TTL touches
        ...
    }

The setting is dynamic, so a running cluster can take it without a restart:
``asinfo -v "set-config:context=namespace;id=lmcache;default-read-touch-ttl-pct=80"``.

The adapter decides which reads may touch, so that an object's records live
and die together:

- a **load** follows the namespace setting, and it reads the meta record and
  every segment, so all of them are extended together;
- a **lookup** never touches.  It reads only the meta record, and extending
  that alone would leave a meta record whose segments have expired: a lookup
  hit that cannot be loaded.

Objects loaded through the RDMA pipelined path are read by the server itself,
which does not apply read-touch, so those loads do not extend a TTL.

**Environment variable fallbacks.**  When the corresponding config value is
empty, these environment variables are used: ``LMCACHE_AEROSPIKE_HOSTS``,
``LMCACHE_AEROSPIKE_NAMESPACE``, ``LMCACHE_AEROSPIKE_SET``,
``LMCACHE_AEROSPIKE_USERNAME``, ``LMCACHE_AEROSPIKE_PASSWORD``.

**Configuration examples:**

.. code-block:: bash

    # Basic single-node Community Edition
    --l2-adapter '{"type": "aerospike", "hosts": "127.0.0.1:3000", "namespace": "lmcache", "set_name": "kv_chunks", "num_workers": 8}'

    # Multi-node seed list with capacity tracking for eviction
    --l2-adapter '{"type": "aerospike", "hosts": "10.0.0.1:3000,10.0.0.2:3000", "namespace": "lmcache", "num_workers": 16, "max_capacity_gb": 512}'

    # Enterprise Edition with authentication
    --l2-adapter '{"type": "aerospike", "hosts": "as.internal:3000", "namespace": "lmcache", "username": "lmcache", "password": "secret"}'
