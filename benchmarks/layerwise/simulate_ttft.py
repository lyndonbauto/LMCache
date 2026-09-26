#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Simulated time-to-first-token for layerwise vs whole-request KV loading.

A first, deliberately modest answer to Track B's B8: how much of a remote KV
fetch does layer-at-a-time loading hide behind prefill compute, on this GPU?

One simulated prefill is ``layers`` layers, each with ``layer_bytes`` of KV
spread over ``chunks`` memory objects (one per LMCache chunk) of ``planes``
K/V planes each. Every layer passes through three real stages:

1. **Remote arrival**, on a precomputed schedule standing in for the RDMA
   transport (no network is involved). Arrival intervals have median
   ``remote_ms``; with ``arrival_p99_ratio`` above 1 they are drawn from a
   seeded lognormal whose 99th percentile is that multiple of the median, so a
   slow layer delays every later one, as on a busy link.
2. **Host-to-device copy**, real pinned H2D copies on a transfer stream. A
   layer is ``chunks * planes`` separate range copies, as the production
   per-layer staging does for the ``(kv, L, S, H)`` object layout.
3. **Compute**, real GPU matmuls calibrated to ``compute_ms`` per layer, on a
   compute stream, plus a read of the layer's KV so compute depends on it.

Three modes are timed from the same start instant until the last layer's
compute finishes:

- ``barrier`` -- layerwise off, as MP mode works today: wait for every layer
  to arrive, copy each object whole (one copy per chunk), then compute every
  layer.
- ``streamed copy`` -- copy each layer as soon as it arrives, but still wait
  for every copy before computing anything. This needs no per-layer waits in
  vLLM, so it isolates how much of the saving comes merely from overlapping
  the copy with the fetch.
- ``layerwise`` -- the real Track B path: ``LayerArrivalPump`` drives
  ``MultiprocessLayerLoadSink`` from a timed fake transport, the launcher
  copies one layer and publishes it through the real ``LayerProgressRecord``
  and event pool, and the "worker" waits per layer with the real
  ``LayerProgressWaiter`` before computing it. What it saves beyond
  ``streamed copy`` is the part only per-layer attention waits can deliver.

Both roles run as threads in one process, so no CUDA IPC is needed (it is not
available under WSL2). The production paged-KV kernel is not used; copies
land in a device mirror of the host objects.

Measured times are printed next to a three-stage pipeline model
(:func:`modeled_ttft_ms`) so disagreements are visible.

Usage::

    python benchmarks/layerwise/simulate_ttft.py
    python benchmarks/layerwise/simulate_ttft.py --arrival-p99-ratio 2.0
    python benchmarks/layerwise/simulate_ttft.py --repeats 9 --json out.json

``--chunk-tokens <prompt tokens> --planes 1 --arrival-p99-ratio 1.0``
reproduces the first version: one copy per layer, fixed arrivals.
"""

# Standard
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
import argparse
import itertools
import json
import math
import random
import statistics
import threading
import time

# Third Party
import torch

# First Party
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerNotInPlanError,
    LayerwiseContractError,
    SlotPlacement,
    StaleGenerationError,
)
from lmcache.v1.layerwise.contract import NO_GENERATION
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
    LayerProgressWaiter,
    WorkerComputeLayerLaunchEventPool,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink
from lmcache.v1.platform.base.event_ipc import get_event_ipc_backend

#: Llama-3-8B KV per token per layer (8 KV heads x 128 dim x K,V x fp16).
LLAMA3_8B_KV_BYTES_PER_TOKEN_PER_LAYER = 4 * 1024

#: Remote bandwidths measured in the M0 sweep (layerwise_transfer_data_model).
NIC_CEILING_BYTES_PER_S = 12.2e9
SINGLE_OBJECT_BYTES_PER_S = 1.88e9

#: LMCache's default chunk size, in tokens; one memory object per chunk.
DEFAULT_CHUNK_TOKENS = 256

#: z-score of the 99th percentile of a standard normal distribution.
_Z_P99 = 2.3263478740408408


@dataclass(frozen=True)
class Scenario:
    """One operating point to simulate.

    Attributes:
        name: Short label for reports.
        layers: Number of transformer layers.
        layer_bytes: KV bytes per layer for the whole prompt.
        remote_ms: Median time for one layer to arrive from the remote store.
        compute_ms: Target GPU compute time per layer.
        chunks: Memory objects the prompt's KV is split into.
        planes: K/V planes per layer in each object (2 for the
            ``(kv, L, S, H)`` layout, 1 for ``(L, S, H)``).
        arrival_p99_ratio: 99th-percentile arrival interval over the median;
            1.0 means every interval is exactly ``remote_ms``.
        seed: Seed for the arrival jitter, so runs are reproducible.
    """

    name: str
    layers: int
    layer_bytes: int
    remote_ms: float
    compute_ms: float
    chunks: int = 1
    planes: int = 1
    arrival_p99_ratio: float = 1.0
    seed: int = 0

    @property
    def copies_per_layer(self) -> int:
        """Range copies needed to stage one layer."""
        return self.chunks * self.planes

    @property
    def piece_bytes(self) -> int:
        """Bytes in one plane of one layer of one object."""
        return self.layer_bytes // self.copies_per_layer


@dataclass(frozen=True)
class ModeledTtft:
    """Pipeline-model TTFT for each mode, in milliseconds."""

    barrier_ms: float
    streamed_copy_ms: float
    layerwise_ms: float


@dataclass(frozen=True)
class _RunTiming:
    """One timed request.

    Attributes:
        total_ms: Request start to the last layer's compute finishing.
        compute_phase_ms: GPU time of the compute phase, when the mode runs
            compute as one uninterrupted phase; ``0.0`` otherwise.
    """

    total_ms: float
    compute_phase_ms: float


@dataclass(frozen=True)
class ScenarioResult:
    """Measured and modelled TTFT for one scenario, in milliseconds.

    Attributes:
        scenario: The operating point.
        last_arrival_ms: When the last layer arrives, after jitter.
        h2d_ms: Measured time to stage one layer (all its range copies).
        barrier_copy_ms: Measured time to copy every object whole.
        compute_ms: Per-layer compute measured *inside* the barrier and
            streamed-copy runs (median). This, not a warm calibration, drives
            the model, so clock or thermal drift during the runs shows up in
            both the measurement and the model instead of only inflating the
            baseline.
        warm_compute_ms: Per-layer compute from the calibration run before the
            timed runs. A large gap to ``compute_ms`` means the GPU slowed down
            during the runs.
        barrier_ms: Median measured TTFT with layerwise off.
        streamed_copy_ms: Median measured TTFT with copies overlapped with the
            fetch but compute still waiting for everything.
        layerwise_ms: Median measured TTFT with layerwise on.
        layerwise_range_ms: ``(min, max)`` of the layerwise runs.
        model: The pipeline model for all three modes.
    """

    scenario: Scenario
    last_arrival_ms: float
    h2d_ms: float
    barrier_copy_ms: float
    compute_ms: float
    warm_compute_ms: float
    barrier_ms: float
    streamed_copy_ms: float
    layerwise_ms: float
    layerwise_range_ms: tuple[float, float]
    model: ModeledTtft

    @property
    def ratio(self) -> float:
        """Measured transfer-to-compute ratio per layer (median arrival)."""
        return self.scenario.remote_ms / self.compute_ms

    @property
    def saving_ms(self) -> float:
        """Measured TTFT removed by layerwise loading, vs the barrier."""
        return self.barrier_ms - self.layerwise_ms

    @property
    def copy_overlap_saving_ms(self) -> float:
        """Share of the saving that overlapping copies with the fetch gets."""
        return self.barrier_ms - self.streamed_copy_ms

    @property
    def compute_overlap_saving_ms(self) -> float:
        """Share of the saving only per-layer compute waits deliver."""
        return self.streamed_copy_ms - self.layerwise_ms


def default_scenarios(
    layers: int = 32,
    prompt_tokens: int = 2048,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    planes: int = 2,
    arrival_p99_ratio: float = 1.0,
) -> tuple[Scenario, ...]:
    """Return the operating points the design documents reason about.

    Compute per layer is set from the transfer-to-compute ratios in
    ``layerwise_transfer_data_model.md`` rather than from this GPU's speed,
    because the ratio, not the absolute speed, decides the overlap.

    Args:
        layers: Number of transformer layers.
        prompt_tokens: Tokens whose KV is fetched.
        chunk_tokens: Tokens per memory object; must divide ``prompt_tokens``.
        planes: K/V planes per layer in each object.
        arrival_p99_ratio: Arrival jitter, as in :class:`Scenario`.

    Returns:
        The scenarios, in report order.

    Raises:
        ValueError: If ``chunk_tokens`` does not divide ``prompt_tokens``.
    """
    if chunk_tokens <= 0 or prompt_tokens % chunk_tokens:
        raise ValueError(
            f"chunk_tokens {chunk_tokens} must divide prompt_tokens {prompt_tokens}"
        )
    layer_bytes = prompt_tokens * LLAMA3_8B_KV_BYTES_PER_TOKEN_PER_LAYER
    line_rate_ms = layer_bytes / NIC_CEILING_BYTES_PER_S * 1e3
    single_object_ms = layer_bytes / SINGLE_OBJECT_BYTES_PER_S * 1e3
    shape = {
        "chunks": prompt_tokens // chunk_tokens,
        "planes": planes,
        "arrival_p99_ratio": arrival_p99_ratio,
    }
    return (
        Scenario("line rate, compute-bound (ratio 0.31)", layers, layer_bytes,
                 line_rate_ms, line_rate_ms / 0.31, **shape),
        Scenario("line rate, balanced (ratio 1.0)", layers, layer_bytes,
                 line_rate_ms, line_rate_ms, **shape),
        Scenario("single-object rate, transfer-bound (ratio 1.98)", layers,
                 layer_bytes, single_object_ms, single_object_ms / 1.98, **shape),
        Scenario("near-complete hit, little prefill (ratio 10)", layers,
                 layer_bytes, line_rate_ms, line_rate_ms / 10.0, **shape),
    )  # fmt: skip


def arrival_offsets_ms(
    layers: int, remote_ms: float, p99_ratio: float = 1.0, seed: int = 0
) -> tuple[float, ...]:
    """Return when each layer arrives, in milliseconds after the request.

    Arrivals are the running sum of per-layer intervals, so a slow layer
    delays every later layer. Intervals have median ``remote_ms``. With
    ``p99_ratio == 1`` every interval is exactly ``remote_ms``; above 1 they
    are lognormal with the given 99th-percentile-to-median ratio, which also
    raises their mean by ``exp(sigma**2 / 2)``.

    Args:
        layers: Number of layers.
        remote_ms: Median arrival interval.
        p99_ratio: 99th-percentile interval over the median; at least 1.
        seed: Seed for the jitter.

    Returns:
        Non-decreasing arrival offsets, one per layer in ascending layer order.

    Raises:
        ValueError: If ``layers`` is not positive, ``remote_ms`` is negative
            or ``p99_ratio`` is below 1.
    """
    if layers <= 0:
        raise ValueError(f"layers must be positive, got {layers}")
    if remote_ms < 0:
        raise ValueError(f"remote_ms must not be negative, got {remote_ms}")
    if p99_ratio < 1.0:
        raise ValueError(f"p99_ratio must be at least 1, got {p99_ratio}")
    if p99_ratio == 1.0 or remote_ms == 0:
        intervals = [remote_ms] * layers
    else:
        rng = random.Random(seed)
        sigma = math.log(p99_ratio) / _Z_P99
        mu = math.log(remote_ms)
        intervals = [rng.lognormvariate(mu, sigma) for _ in range(layers)]
    return tuple(itertools.accumulate(intervals))


def modeled_ttft_ms(
    arrivals_ms: Sequence[float],
    h2d_ms: float,
    compute_ms: float,
    barrier_copy_ms: float,
) -> ModeledTtft:
    """Model all three modes as deterministic stages.

    Layer ``i`` arrives at ``arrivals_ms[i]``. Copies are serial on one
    stream; compute is serial on another. Polling and launch overheads are
    ignored.

    - Barrier: the last layer arrives, then every object is copied whole
      (``barrier_copy_ms``), then all compute.
    - Streamed copy: each layer's copy starts once it has arrived and the
      previous copy finished; compute starts after the last copy.
    - Layerwise: as streamed copy, but each layer's compute starts as soon as
      its own copy and the previous layer's compute have finished.

    Args:
        arrivals_ms: Non-decreasing arrival time of each layer.
        h2d_ms: Time to stage one layer.
        compute_ms: Compute time per layer.
        barrier_copy_ms: Time to copy every object whole.

    Returns:
        The modelled TTFT of each mode.

    Raises:
        ValueError: If there are no layers, arrivals decrease, or any time is
            negative.
    """
    if not arrivals_ms:
        raise ValueError("at least one layer must arrive")
    if min(arrivals_ms[0], h2d_ms, compute_ms, barrier_copy_ms) < 0:
        raise ValueError("stage times must not be negative")
    if any(later < earlier for earlier, later in itertools.pairwise(arrivals_ms)):
        raise ValueError("arrivals must not decrease")
    layers = len(arrivals_ms)
    copy_done = 0.0
    compute_done = 0.0
    for arrived in arrivals_ms:
        copy_done = max(arrived, copy_done) + h2d_ms
        compute_done = max(copy_done, compute_done) + compute_ms
    return ModeledTtft(
        barrier_ms=arrivals_ms[-1] + barrier_copy_ms + layers * compute_ms,
        streamed_copy_ms=copy_done + layers * compute_ms,
        layerwise_ms=compute_done,
    )


class _TimedArrivalSource:
    """A fake transport whose layers become resident on a wall-clock schedule.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerArrivalSource`.
    Layer ``i`` of the plan (in ascending order) is resident from
    ``start + arrivals_s[i]``.
    """

    def __init__(self, start: float, arrivals_s: Sequence[float]) -> None:
        """Fix the arrival schedule.

        Args:
            start: ``time.perf_counter()`` at which the request started.
            arrivals_s: Seconds after ``start`` at which each layer arrives,
                one per plan layer in ascending order.
        """
        self._start = start
        self._arrivals_s = tuple(arrivals_s)
        self._generation = NO_GENERATION
        self._arrival: dict[int, float] = {}

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Start the scheduled fetch; returns generation 1."""
        if self._generation != NO_GENERATION:
            raise LayerwiseContractError("a fetch is already active")
        layer_ids = plan.layer_ids()
        if len(layer_ids) != len(self._arrivals_s):
            raise LayerwiseContractError(
                f"plan has {len(layer_ids)} layers, schedule has "
                f"{len(self._arrivals_s)}"
            )
        self._arrival = {
            layer_id: self._start + offset
            for layer_id, offset in zip(layer_ids, self._arrivals_s, strict=True)
        }
        self._generation = 1
        return self._generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Report a layer resident once its scheduled arrival has passed."""
        if generation != self._generation or generation == NO_GENERATION:
            raise StaleGenerationError(f"generation {generation} is not active")
        if layer_id not in self._arrival:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch")
        if time.perf_counter() >= self._arrival[layer_id]:
            return LayerArrivalStatus.RESIDENT
        return LayerArrivalStatus.PENDING

    def finish_fetch(self, generation: int) -> None:
        """Release the fetch."""
        if generation != self._generation:
            raise StaleGenerationError(f"generation {generation} is not active")
        self._generation = NO_GENERATION

    def abandon_fetch(self, generation: int) -> None:
        """Release the fetch; tolerates an inactive generation."""
        if generation == self._generation:
            self._generation = NO_GENERATION


class _SimulationLauncher:
    """A :class:`LayerLauncher` staging one layer's range copies per launch.

    Publishes each layer exactly as production does: copy on the transfer
    stream, record that ordinal's event on the same stream, then advance the
    watermark.
    """

    def __init__(
        self,
        copy_layer: Callable[[int], None],
        transfer_stream: torch.cuda.Stream,
        progress: LayerProgressRecord,
        event_pool: DaemonLayerLaunchEventPool,
        retrieve_generation: int,
    ) -> None:
        """Bind one simulated retrieve.

        Args:
            copy_layer: Enqueues one layer's copies on the current stream.
            transfer_stream: Stream the copies and event records run on.
            progress: Progress record the worker waits on.
            event_pool: Per-ordinal events recorded after each copy.
            retrieve_generation: Generation the worker waits on.
        """
        self._copy_layer = copy_layer
        self._transfer_stream = transfer_stream
        self._progress = progress
        self._event_pool = event_pool
        self._retrieve_generation = retrieve_generation
        self._next_ordinal = 0

    def begin(self) -> None:
        """Publish the retrieve generation."""
        self._progress.begin_retrieve(self._retrieve_generation)

    def launch_layer(self, layer_id: int) -> None:
        """Copy one layer, record its event, then advance the watermark."""
        with torch.cuda.stream(self._transfer_stream):
            self._copy_layer(layer_id)
        self._event_pool.record_ordinal(self._next_ordinal, self._transfer_stream)
        self._next_ordinal += 1
        self._progress.report_launch_recorded(self._next_ordinal)

    def mark_failed(self) -> None:
        """Publish a failure for this retrieve."""
        self._progress.mark_retrieve_failed()


class _Workload:
    """GPU buffers, streams and calibrated compute for one scenario.

    KV is held as ``[chunks, planes, layers, elements]``: object ``c`` is
    ``host[c]``, laid out ``(kv, L, S*H)`` like a production memory object, so
    one plane of one layer is a contiguous range.
    """

    def __init__(self, scenario: Scenario) -> None:
        """Allocate KV buffers and calibrate compute to the scenario's target.

        Args:
            scenario: The operating point.

        Raises:
            ValueError: If ``layer_bytes`` does not split into whole fp16
                values per piece.
        """
        if scenario.layer_bytes % (2 * scenario.copies_per_layer):
            raise ValueError(
                "layer_bytes must split into whole fp16 values per plane per chunk"
            )
        self.scenario = scenario
        self.device_id = torch.device("cuda:0")
        shape = (
            scenario.chunks,
            scenario.planes,
            scenario.layers,
            scenario.piece_bytes // 2,
        )
        self.host = torch.ones(shape, dtype=torch.float16, pin_memory=True)
        self.device = torch.zeros(shape, dtype=torch.float16, device=self.device_id)
        self.arrivals_ms = arrival_offsets_ms(
            scenario.layers,
            scenario.remote_ms,
            scenario.arrival_p99_ratio,
            scenario.seed,
        )
        self.transfer_stream = torch.cuda.Stream(device=self.device_id)
        self.compute_stream = torch.cuda.Stream(device=self.device_id)
        self._compute_layer = _calibrated_compute(
            scenario.compute_ms, self.device[-1, -1, :, :1], self.compute_stream
        )

    def copy_layer(self, layer_id: int) -> None:
        """Enqueue one layer's range copies (every chunk, every plane)."""
        for chunk in range(self.scenario.chunks):
            for plane in range(self.scenario.planes):
                self.device[chunk, plane, layer_id].copy_(
                    self.host[chunk, plane, layer_id], non_blocking=True
                )

    def copy_whole_objects(self) -> None:
        """Enqueue one whole-object copy per chunk, as the barrier path does."""
        for chunk in range(self.scenario.chunks):
            self.device[chunk].copy_(self.host[chunk], non_blocking=True)

    def compute(self, layer_id: int) -> None:
        """Enqueue one layer's compute on the current stream."""
        self._compute_layer(layer_id)

    def measure_h2d_ms(self, repeats: int = 20) -> float:
        """Return the median time to stage one layer."""
        return _median_gpu_ms(lambda: self.copy_layer(0), self.transfer_stream, repeats)

    def measure_barrier_copy_ms(self, repeats: int = 5) -> float:
        """Return the median time to copy every object whole."""
        return _median_gpu_ms(self.copy_whole_objects, self.transfer_stream, repeats)

    def measure_compute_ms(self, repeats: int = 20) -> float:
        """Return the median time of one layer's compute."""
        return _median_gpu_ms(lambda: self.compute(0), self.compute_stream, repeats)

    def run_barrier(self) -> _RunTiming:
        """Time one request with layerwise off."""
        torch.cuda.synchronize()
        start = time.perf_counter()
        _sleep_until(start + self.arrivals_ms[-1] / 1e3)
        copied = torch.cuda.Event()
        with torch.cuda.stream(self.transfer_stream):
            self.copy_whole_objects()
            copied.record()
        return self._compute_all_after(copied, start)

    def run_streamed_copy(self) -> _RunTiming:
        """Time one request copying each layer on arrival; compute waits all."""
        torch.cuda.synchronize()
        start = time.perf_counter()
        copied = torch.cuda.Event()
        with torch.cuda.stream(self.transfer_stream):
            for layer_id, arrival_ms in enumerate(self.arrivals_ms):
                _sleep_until(start + arrival_ms / 1e3)
                self.copy_layer(layer_id)
            copied.record()
        return self._compute_all_after(copied, start)

    def run_layerwise(self, retrieve_generation: int) -> float:
        """Time one request through the real Track B path; returns ms.

        Args:
            retrieve_generation: Positive generation, unique per run.

        Raises:
            RuntimeError: If the pump thread failed.
        """
        scenario = self.scenario
        schedule = LayerwiseSchedule([list(range(scenario.layers))])
        backend = get_event_ipc_backend(self.device_id)
        events = [backend.create_event(self.device_id) for _ in range(scenario.layers)]
        progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
        daemon_pool = DaemonLayerLaunchEventPool(events, backend, scenario.layers)
        waiter = LayerProgressWaiter(
            progress,
            WorkerComputeLayerLaunchEventPool(events, backend, scenario.layers),
            wait_timeout_seconds=30.0,
        )
        plan = _fetch_plan(scenario)
        errors: list[BaseException] = []

        torch.cuda.synchronize()
        start = time.perf_counter()
        sink = MultiprocessLayerLoadSink.for_retrieve(
            schedule,
            _SimulationLauncher(
                self.copy_layer,
                self.transfer_stream,
                progress,
                daemon_pool,
                retrieve_generation,
            ),
        )
        pump = LayerArrivalPump(
            _TimedArrivalSource(start, [ms / 1e3 for ms in self.arrivals_ms]), sink
        )

        def run_pump() -> None:
            try:
                pump.run(plan)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=run_pump, name="layerwise-pump")
        thread.start()
        with torch.cuda.stream(self.compute_stream):
            for layer_id in range(scenario.layers):
                waiter.wait_for_layer(retrieve_generation, layer_id, schedule)
                self.compute(layer_id)
        self.compute_stream.synchronize()
        elapsed_ms = (time.perf_counter() - start) * 1e3
        thread.join()
        if errors:
            raise RuntimeError(f"pump failed: {errors[0]!r}") from errors[0]
        return elapsed_ms

    def _compute_all_after(self, copied: torch.cuda.Event, start: float) -> _RunTiming:
        """Compute every layer once ``copied`` completes; time the whole run.

        Args:
            copied: Event recorded after the last copy.
            start: ``time.perf_counter()`` at request start.

        Returns:
            The run's total time and the GPU time of its compute phase.
        """
        phase_start = torch.cuda.Event(enable_timing=True)
        phase_end = torch.cuda.Event(enable_timing=True)
        with torch.cuda.stream(self.compute_stream):
            self.compute_stream.wait_event(copied)
            phase_start.record()
            for layer_id in range(self.scenario.layers):
                self.compute(layer_id)
            phase_end.record()
        self.compute_stream.synchronize()
        total_ms = (time.perf_counter() - start) * 1e3
        return _RunTiming(total_ms, phase_start.elapsed_time(phase_end))


def _fetch_plan(scenario: Scenario) -> LayerFetchPlan:
    """Return one slot per (layer, chunk, plane), offsets as in ``_Workload``."""
    piece = scenario.piece_bytes
    return LayerFetchPlan(
        tuple(
            SlotPlacement(
                layer_id=layer_id,
                chunk_id=chunk,
                node_index=0,
                record_key=f"sim|{chunk}|{layer_id}|{plane}",
                plane=plane,
                piece=0,
                offset=((chunk * scenario.planes + plane) * scenario.layers + layer_id)
                * piece,
                length=piece,
            )
            for layer_id in range(scenario.layers)
            for chunk in range(scenario.chunks)
            for plane in range(scenario.planes)
        ),
        ("simulated-node",),
    )


def _sleep_until(deadline: float) -> None:
    """Block until ``time.perf_counter()`` reaches ``deadline``."""
    remaining = deadline - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)


def _median_gpu_ms(
    enqueue: Callable[[], None], stream: torch.cuda.Stream, repeats: int
) -> float:
    """Return the median GPU time of ``enqueue``'s work on ``stream``."""
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    samples: list[float] = []
    with torch.cuda.stream(stream):
        for _ in range(repeats):
            start.record()
            enqueue()
            end.record()
            end.synchronize()
            samples.append(start.elapsed_time(end))
    return statistics.median(samples)


def _calibrated_compute(
    target_ms: float, kv_probe: torch.Tensor, stream: torch.cuda.Stream
) -> Callable[[int], None]:
    """Build a per-layer compute step that takes about ``target_ms``.

    Each step reads one element of the layer's KV (so it is ordered after the
    layer's copy on the device) and then runs a chain of square fp16 matmuls.
    The matmul size is the largest that still needs at least two matmuls per
    step, which keeps launch overhead small while allowing fine-grained
    targets.

    Args:
        target_ms: Desired GPU time per layer.
        kv_probe: ``[layers, 1]`` device view with one KV element per layer.
        stream: Stream to calibrate on.

    Returns:
        A function enqueuing one layer's compute on the current stream.
    """
    device = kv_probe.device
    chosen_size, per_matmul_ms = 128, 0.0
    for size in (2048, 1024, 512, 256, 128):
        chosen_size, per_matmul_ms = size, _matmul_ms(size, device, stream)
        if per_matmul_ms * 2 <= target_ms:
            break
    repeats = max(1, round(target_ms / per_matmul_ms))
    left = torch.randn((chosen_size, chosen_size), dtype=torch.float16, device=device)
    right = torch.randn((chosen_size, chosen_size), dtype=torch.float16, device=device)
    scratch = torch.empty(1, dtype=torch.float32, device=device)

    def compute_layer(layer_id: int) -> None:
        scratch.copy_(kv_probe[layer_id].float())
        product = left
        for _ in range(repeats):
            product = torch.mm(product, right)

    return compute_layer


def _matmul_ms(size: int, device: torch.device, stream: torch.cuda.Stream) -> float:
    """Return the median time of one ``size x size`` fp16 matmul."""
    left = torch.randn((size, size), dtype=torch.float16, device=device)
    right = torch.randn((size, size), dtype=torch.float16, device=device)
    with torch.cuda.stream(stream):
        for _ in range(5):
            torch.mm(left, right)
    return _median_gpu_ms(lambda: torch.mm(left, right), stream, 20)


def run_scenario(scenario: Scenario, repeats: int) -> ScenarioResult:
    """Measure one scenario in all three modes.

    Args:
        scenario: The operating point.
        repeats: Timed runs per mode, after one warm-up run each.

    Returns:
        Medians for every mode plus the pipeline model.
    """
    workload = _Workload(scenario)
    h2d_ms = workload.measure_h2d_ms()
    barrier_copy_ms = workload.measure_barrier_copy_ms()
    warm_compute_ms = workload.measure_compute_ms()
    workload.run_barrier()
    workload.run_streamed_copy()
    workload.run_layerwise(retrieve_generation=1)
    barrier: list[_RunTiming] = []
    streamed: list[_RunTiming] = []
    layerwise: list[float] = []
    # Interleave the modes so slow drift (clocks, thermals) hits all equally.
    for run in range(repeats):
        barrier.append(workload.run_barrier())
        streamed.append(workload.run_streamed_copy())
        layerwise.append(workload.run_layerwise(retrieve_generation=run + 2))
    compute_ms = statistics.median(
        timing.compute_phase_ms / scenario.layers for timing in barrier + streamed
    )
    return ScenarioResult(
        scenario=scenario,
        last_arrival_ms=workload.arrivals_ms[-1],
        h2d_ms=h2d_ms,
        barrier_copy_ms=barrier_copy_ms,
        compute_ms=compute_ms,
        warm_compute_ms=warm_compute_ms,
        barrier_ms=statistics.median(timing.total_ms for timing in barrier),
        streamed_copy_ms=statistics.median(timing.total_ms for timing in streamed),
        layerwise_ms=statistics.median(layerwise),
        layerwise_range_ms=(min(layerwise), max(layerwise)),
        model=modeled_ttft_ms(
            workload.arrivals_ms, h2d_ms, compute_ms, barrier_copy_ms
        ),
    )


def format_report(results: list[ScenarioResult]) -> str:
    """Render results as a fixed-width table.

    Args:
        results: One entry per scenario.

    Returns:
        The report text.
    """
    lines = [
        "Per layer (ms): median remote arrival, H2D staging, compute; measured "
        "ratio. last = last layer's arrival (ms); bcopy = whole-object copy of "
        "everything (ms).",
        "TTFT (ms): measured median (pipeline model).",
        "",
        f"{'scenario':<48} {'remote':>6} {'h2d':>5} {'comp':>5} {'ratio':>5} "
        f"{'last':>6} {'bcopy':>5} "
        f"{'barrier':>13} {'streamed':>13} {'layerwise':>13} "
        f"{'saved':>6} {'copy':>6} {'comp':>6}",
    ]
    for r in results:
        m = r.model
        lines.append(
            f"{r.scenario.name:<48} {r.scenario.remote_ms:>6.3f} {r.h2d_ms:>5.2f} "
            f"{r.compute_ms:>5.2f} {r.ratio:>5.2f} "
            f"{r.last_arrival_ms:>6.1f} {r.barrier_copy_ms:>5.1f} "
            f"{r.barrier_ms:>6.1f}({m.barrier_ms:>5.1f}) "
            f"{r.streamed_copy_ms:>6.1f}({m.streamed_copy_ms:>5.1f}) "
            f"{r.layerwise_ms:>6.1f}({m.layerwise_ms:>5.1f}) "
            f"{r.saving_ms:>6.1f} {r.copy_overlap_saving_ms:>6.1f} "
            f"{r.compute_overlap_saving_ms:>6.1f}"
        )
    lines.append("")
    lines.append(
        "saved = barrier - layerwise; copy = barrier - streamed (no vLLM changes "
        "needed); comp = streamed - layerwise (needs per-layer attention waits)."
    )
    for r in results:
        drift = r.compute_ms / r.warm_compute_ms - 1.0
        if abs(drift) > 0.10:
            cause = (
                "the GPU slowed during the runs (clocks/thermals)"
                if drift > 0
                else "back-to-back tiny kernels overlap their launch cost"
            )
            lines.append(
                f"note: '{r.scenario.name}': in-run compute {r.compute_ms:.2f} ms "
                f"vs warm {r.warm_compute_ms:.2f} ms ({drift:+.0%}); {cause}. "
                "The model uses the in-run value."
            )
    return "\n".join(lines)


def main() -> None:
    """Run the default scenarios and print (and optionally save) the report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--layers", type=int, default=32)
    parser.add_argument("--prompt-tokens", type=int, default=2048)
    parser.add_argument("--chunk-tokens", type=int, default=DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--planes", type=int, default=2)
    parser.add_argument("--arrival-p99-ratio", type=float, default=1.0)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--json", type=str, default="", help="write results here")
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("a CUDA GPU is required")

    scenarios = default_scenarios(
        args.layers,
        args.prompt_tokens,
        args.chunk_tokens,
        args.planes,
        args.arrival_p99_ratio,
    )
    results = [run_scenario(scenario, args.repeats) for scenario in scenarios]
    first = scenarios[0]
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"{args.layers} layers, {args.prompt_tokens} prompt tokens, "
          f"{first.chunks} chunks x {first.planes} planes = "
          f"{first.copies_per_layer} copies/layer of {first.piece_bytes} B, "
          f"arrival p99/median {args.arrival_p99_ratio:g}, "
          f"median of {args.repeats} runs per mode")  # fmt: skip
    print(format_report(results))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        **asdict(r),
                        "ratio": r.ratio,
                        "saving_ms": r.saving_ms,
                        "copy_overlap_saving_ms": r.copy_overlap_saving_ms,
                        "compute_overlap_saving_ms": r.compute_overlap_saving_ms,
                    }
                    for r in results
                ],
                handle,
                indent=2,
            )


if __name__ == "__main__":
    main()
