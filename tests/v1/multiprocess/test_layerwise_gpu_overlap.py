# SPDX-License-Identifier: Apache-2.0
"""Single-process GPU proof of the Track B per-layer overlap primitive.

Track B's job is to copy each KV layer to the GPU as it lands and let the
serving engine consume layer ``N`` while later layers are still in flight. The
full production path splits that across two processes (the LMCache daemon and
the vLLM worker) and hands the completion signal over CUDA IPC. CUDA IPC is not
available under WSL2, so this harness deliberately runs both roles as two
threads inside **one** process and one CUDA context, where no IPC is involved.

What this still proves on a real GPU:

- real per-layer host-to-device copies land byte-correct, and
- a layer becomes consumable *before* the whole transfer has finished --
  i.e. the mechanism is a pipeline, not a barrier.

What it intentionally does **not** cover: exporting a CUDA event handle from
one process and importing it in another. That single hop is the only part that
requires cross-process CUDA IPC and must be validated on native Linux (see the
GPU CI job), not under WSL.

It reuses the production classes (:class:`LayerwiseSchedule`,
:class:`LayerProgressRecord`, :class:`LayerProgressWaiter`,
:class:`DaemonLayerLaunchEventPool`, :class:`WorkerComputeLayerLaunchEventPool`)
so a regression in the real overlap machinery fails here rather than only in a
bespoke mock. In a single process the daemon-recorded events are the same
objects the worker waits on, so both pools are constructed over one shared
event list.
"""

# Standard
import threading
import time

# Third Party
import pytest
import torch

# First Party
from lmcache import torch_dev, torch_device_type
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
    LayerProgressWaiter,
    WorkerComputeLayerLaunchEventPool,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.platform.base.event_ipc import get_event_ipc_backend

#: Number of model layers the harness pipelines.
_NUM_LAYERS = 8

#: Float32 elements per layer buffer (1 MiB per layer at fp32).
_LAYER_WIDTH = 256 * 1024

#: Simulated gap between successive layer arrivals on the daemon side. The
#: worker should observe layers becoming ready roughly this far apart, which is
#: what makes the overlap measurable rather than a single end-of-transfer jump.
_ARRIVAL_PACE_SECONDS = 0.03

#: Generation tag for the single retrieve this harness drives. Non-zero because
#: ``0`` is reserved for "no active retrieve".
_GENERATION = 1

#: Per-layer wait ceiling; generous so a slow shared runner never flakes.
_WAIT_TIMEOUT_SECONDS = 30.0


def _run_daemon(
    schedule: LayerwiseSchedule,
    record: LayerProgressRecord,
    event_pool: DaemonLayerLaunchEventPool,
    source_host: torch.Tensor,
    destination_device: torch.Tensor,
    transfer_stream: "torch.cuda.Stream",
    errors: list[BaseException],
) -> None:
    """Enqueue one paced host-to-device copy per layer, recording each ordinal.

    Mirrors the daemon side of the layerwise retrieve: for every launch in
    schedule order it stages the layer's bytes onto the transfer stream,
    records that ordinal's completion event on the same stream, then bumps the
    shared watermark. The deliberate sleep before each layer simulates a slow
    remote fetch so the worker can observe layers arriving one at a time.

    Args:
        schedule: Global per-layer launch order.
        record: Shared progress record to publish the watermark into.
        event_pool: Daemon-owned events recorded per ordinal on the stream.
        source_host: Pinned host tensor, row ``l`` holding layer ``l``'s bytes.
        destination_device: GPU tensor written row by row as layers land.
        transfer_stream: CUDA stream the copies and event records run on.
        errors: Collects any exception so the main thread can re-raise it.
    """
    try:
        torch.cuda.set_device(destination_device.device)
        record.begin_retrieve(_GENERATION)
        for ordinal, launch in enumerate(schedule.launches):
            time.sleep(_ARRIVAL_PACE_SECONDS)
            layer_id = launch.layer_id
            with torch.cuda.stream(transfer_stream):
                destination_device[layer_id].copy_(
                    source_host[layer_id], non_blocking=True
                )
            event_pool.record_ordinal(ordinal, transfer_stream)
            record.report_launch_recorded(ordinal + 1)
    except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
        errors.append(exc)


def _run_worker(
    schedule: LayerwiseSchedule,
    waiter: LayerProgressWaiter,
    destination_device: torch.Tensor,
    compute_stream: "torch.cuda.Stream",
    ready_times: dict[int, float],
    observed_values: dict[int, tuple[float, float]],
    errors: list[BaseException],
) -> None:
    """Wait for each layer in order, then read it back on the compute stream.

    Mirrors the worker side: for every layer in ascending order it blocks until
    the shared watermark and completion event say the layer has landed, then
    reads that layer's buffer on the compute stream (which the waiter has
    ordered behind the daemon's copy) and records the value range so the caller
    can assert byte correctness. The wall-clock time each layer becomes ready is
    captured to prove later layers are not required for earlier ones.

    Args:
        schedule: Global per-layer launch order.
        waiter: Worker-side per-layer wait bound to the shared record and pool.
        destination_device: GPU tensor the daemon writes; read here per layer.
        compute_stream: CUDA stream the worker waits and reads on.
        ready_times: Populated with ``layer_id -> monotonic ready time``.
        observed_values: Populated with ``layer_id -> (min, max)`` read back.
        errors: Collects any exception so the main thread can re-raise it.
    """
    try:
        torch.cuda.set_device(destination_device.device)
        with torch.cuda.stream(compute_stream):
            for launch in schedule.launches:
                layer_id = launch.layer_id
                waiter.wait_for_layer(_GENERATION, layer_id, schedule)
                ready_times[layer_id] = time.monotonic()
                host_copy = destination_device[layer_id].to("cpu")
                observed_values[layer_id] = (
                    float(host_copy.min().item()),
                    float(host_copy.max().item()),
                )
    except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
        errors.append(exc)


@pytest.mark.cuda
@pytest.mark.skipif(
    not torch_dev.is_available(),
    reason=f"requires available {torch_device_type} runtime",
)
def test_layers_are_consumable_before_the_transfer_finishes() -> None:
    """A layer is byte-correct and consumable while later layers are outstanding.

    Drives the real layerwise progress machinery across a daemon thread and a
    worker thread sharing one CUDA context. Asserts every layer reads back its
    own value, layers become ready in ascending order, and the first layer is
    ready well before the last -- the property that distinguishes a pipeline
    from a whole-request barrier.
    """
    torch.cuda.init()
    device = torch.device("cuda:0")

    schedule = LayerwiseSchedule([list(range(_NUM_LAYERS))])
    backend = get_event_ipc_backend(device)
    # One event list shared by both pools: in a single process the worker waits
    # on the very events the daemon records, so no IPC export/import is needed.
    events: list[object] = [backend.create_event(device) for _ in range(_NUM_LAYERS)]
    daemon_pool = DaemonLayerLaunchEventPool(events, backend, _NUM_LAYERS)
    worker_pool = WorkerComputeLayerLaunchEventPool(events, backend, _NUM_LAYERS)

    record = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    waiter = LayerProgressWaiter(
        record,
        worker_pool,
        wait_timeout_seconds=_WAIT_TIMEOUT_SECONDS,
    )

    source_host = torch.empty(
        (_NUM_LAYERS, _LAYER_WIDTH), dtype=torch.float32, pin_memory=True
    )
    for layer_id in range(_NUM_LAYERS):
        source_host[layer_id].fill_(float(layer_id + 1))
    destination_device = torch.zeros(
        (_NUM_LAYERS, _LAYER_WIDTH), dtype=torch.float32, device=device
    )

    transfer_stream = torch.cuda.Stream(device=device)
    compute_stream = torch.cuda.Stream(device=device)

    ready_times: dict[int, float] = {}
    observed_values: dict[int, tuple[float, float]] = {}
    daemon_errors: list[BaseException] = []
    worker_errors: list[BaseException] = []

    worker = threading.Thread(
        target=_run_worker,
        name="layerwise-worker",
        args=(
            schedule,
            waiter,
            destination_device,
            compute_stream,
            ready_times,
            observed_values,
            worker_errors,
        ),
    )
    daemon = threading.Thread(
        target=_run_daemon,
        name="layerwise-daemon",
        args=(
            schedule,
            record,
            daemon_pool,
            source_host,
            destination_device,
            transfer_stream,
            daemon_errors,
        ),
    )

    worker.start()
    daemon.start()
    daemon.join(timeout=_WAIT_TIMEOUT_SECONDS + 5.0)
    worker.join(timeout=_WAIT_TIMEOUT_SECONDS + 5.0)

    assert not daemon.is_alive(), "daemon thread did not finish"
    assert not worker.is_alive(), "worker thread did not finish"
    assert daemon_errors == [], f"daemon failed: {daemon_errors}"
    assert worker_errors == [], f"worker failed: {worker_errors}"

    # Every layer read back exactly its own fill value on both ends of the row.
    for layer_id in range(_NUM_LAYERS):
        expected = float(layer_id + 1)
        assert observed_values[layer_id] == (expected, expected), (
            f"layer {layer_id} read back {observed_values[layer_id]}, "
            f"expected ({expected}, {expected})"
        )

    # Layers became ready in ascending order.
    ordered = [ready_times[layer_id] for layer_id in range(_NUM_LAYERS)]
    assert ordered == sorted(ordered), (
        f"layers did not become ready in order: {ordered}"
    )

    # The first layer was consumable well before the last -- proof the worker
    # did not wait for the whole transfer. Half the ideal spread is a wide
    # margin that still fails a barrier (which would show ~0 spread).
    spread = ordered[-1] - ordered[0]
    minimum_spread = _ARRIVAL_PACE_SECONDS * (_NUM_LAYERS - 1) * 0.5
    assert spread >= minimum_spread, (
        f"layers became ready in {spread:.4f}s (< {minimum_spread:.4f}s); "
        "the pipeline collapsed into a barrier"
    )
