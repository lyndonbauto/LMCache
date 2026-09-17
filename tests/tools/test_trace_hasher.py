# SPDX-License-Identifier: Apache-2.0
"""Tests for the offline trace-to-lookup-hash converter.

The interesting assertions are not that JSON round-trips. They are that the
converter reproduces two LMCache behaviours which, if got wrong, would inflate
every hit rate derived from its output:

* hashing is **rolling**, so a hit needs an exact prefix match, and
* trailing partial chunks are **never cached**, so they always miss.

Both are verified end to end by feeding the output through the real simulator
rather than by inspecting hashes, since the hash values themselves are not the
contract -- the resulting hit rate is.
"""

# Standard
from pathlib import Path
import json

# Third Party
import pytest

# First Party
from lmcache.tools.cache_simulator.simulator import (
    compute_kv_bytes_per_chunk,
    load_lookup_events,
    simulate,
)
from lmcache.tools.cache_simulator.trace_hasher import (
    TraceRequest,
    kv_shape,
    read_trace,
    tokenize_requests,
    write_lookup_events,
)
from lmcache.v1.multiprocess.token_hasher import TokenHasher

CHUNK = 4
# A capacity far larger than the trace, so nothing is evicted and the hit rate
# reflects only prefix matching. Eviction is the simulator's concern and is
# already covered by its own tests.
ROOMY_CAPACITY = 1 << 20


def write_trace(path: Path, requests: list[dict[str, object]]) -> None:
    """Write ``requests`` to ``path`` as JSONL."""
    with open(path, "w", encoding="utf-8") as handle:
        for record in requests:
            handle.write(json.dumps(record) + "\n")


def hit_rates(tmp_path: Path, traces: list[list[int]]) -> list[float]:
    """Convert token-ID requests and return the per-request token hit rates.

    Args:
        tmp_path: Directory for intermediate files.
        traces: One token-ID list per request, in arrival order.

    Returns:
        Per-request token hit rate, index-aligned to ``traces``.
    """
    requests = [
        TraceRequest(request_id=f"r{i}", timestamp=float(i), token_ids=t)
        for i, t in enumerate(traces)
    ]
    events_path = tmp_path / "lookup.jsonl"
    hasher = TokenHasher(chunk_size=CHUNK, hash_algorithm="blake3")
    shape = kv_shape(CHUNK, layers=1, kv_heads=1, head_dim=1)
    with open(events_path, "w", encoding="utf-8") as handle:
        write_lookup_events(
            requests=requests,
            token_ids_per_request=traces,
            hasher=hasher,
            model_name="test-model",
            shape=shape,
            dtype="bfloat16",
            handle=handle,
        )
    events = load_lookup_events([events_path])
    results = simulate(
        events,
        cache_capacity_bytes=ROOMY_CAPACITY,
        kv_bytes_per_chunk=compute_kv_bytes_per_chunk(events[0]),
    )
    return results["per_request_token_hit_rates"]


class TestKvShape:
    def test_shape_is_key_and_value_plane_per_layer(self):
        assert kv_shape(256, layers=32, kv_heads=8, head_dim=128) == [64, 256, 1024]

    def test_shape_implies_the_documented_kv_per_token(self):
        # Llama-3-8B geometry in bf16 is 128 KiB of KV per token.
        shape = kv_shape(256, layers=32, kv_heads=8, head_dim=128)
        bytes_per_chunk = shape[0] * shape[1] * shape[2] * 2
        assert bytes_per_chunk / 256 == 128 * 1024

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"chunk_size": 0, "layers": 1, "kv_heads": 1, "head_dim": 1},
            {"chunk_size": 1, "layers": -1, "kv_heads": 1, "head_dim": 1},
            {"chunk_size": 1, "layers": 1, "kv_heads": 0, "head_dim": 1},
            {"chunk_size": 1, "layers": 1, "kv_heads": 1, "head_dim": 0},
        ],
    )
    def test_rejects_non_positive_geometry(self, kwargs):
        with pytest.raises(ValueError):
            kv_shape(**kwargs)


class TestReadTrace:
    def test_reads_token_id_and_prompt_forms(self, tmp_path):
        path = tmp_path / "t.jsonl"
        write_trace(path, [{"token_ids": [1, 2]}, {"prompt": "hi"}])
        assert len(read_trace(path)) == 2

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"token_ids": [1]}\n\n\n{"token_ids": [2]}\n')
        assert len(read_trace(path)) == 2

    def test_rejects_malformed_json(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"token_ids": [1]}\nnot json\n')
        with pytest.raises(ValueError, match="malformed JSON"):
            read_trace(path)

    def test_rejects_request_with_neither_field(self, tmp_path):
        path = tmp_path / "t.jsonl"
        write_trace(path, [{"request_id": "r0"}])
        with pytest.raises(ValueError, match="token_ids.*prompt"):
            read_trace(path)


class TestTokenizeRequests:
    def test_numeric_trace_needs_no_tokenizer(self):
        requests = [TraceRequest(request_id="r0", timestamp=0.0, token_ids=[1, 2, 3])]
        assert tokenize_requests(requests, "") == [[1, 2, 3]]

    def test_text_trace_without_tokenizer_is_rejected(self):
        requests = [TraceRequest(request_id="r0", timestamp=0.0, prompt="hello")]
        with pytest.raises(ValueError, match="--tokenizer is required"):
            tokenize_requests(requests, "")


class TestWriteLookupEvents:
    def test_emits_the_schema_the_simulator_consumes(self, tmp_path):
        path = tmp_path / "e.jsonl"
        traces = [list(range(8))]
        requests = [TraceRequest(request_id="r0", timestamp=0.0, token_ids=traces[0])]
        with open(path, "w", encoding="utf-8") as handle:
            written = write_lookup_events(
                requests=requests,
                token_ids_per_request=traces,
                hasher=TokenHasher(chunk_size=CHUNK),
                model_name="m",
                shape=kv_shape(CHUNK, 1, 1, 1),
                dtype="bfloat16",
                handle=handle,
            )
        assert written == 1
        event = json.loads(path.read_text().strip())
        assert set(event) >= {
            "timestamp",
            "request_id",
            "model_name",
            "chunk_size",
            "seq_len",
            "dtypes",
            "shapes",
            "chunk_hashes",
        }
        # Hashes carry the same "0x"-prefixed hex encoding the server logs.
        assert all(h.startswith("0x") for h in event["chunk_hashes"])
        assert compute_kv_bytes_per_chunk(event) > 0

    def test_rejects_unknown_dtype_rather_than_modelling_zero_bytes(self, tmp_path):
        path = tmp_path / "e.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            with pytest.raises(ValueError, match="unrecognised dtype"):
                write_lookup_events(
                    requests=[
                        TraceRequest(request_id="r0", timestamp=0.0, token_ids=[1])
                    ],
                    token_ids_per_request=[[1]],
                    hasher=TokenHasher(chunk_size=CHUNK),
                    model_name="m",
                    shape=kv_shape(CHUNK, 1, 1, 1),
                    dtype="float9_imaginary",
                    handle=handle,
                )

    def test_seq_len_is_the_whole_prompt_including_the_uncacheable_tail(self, tmp_path):
        path = tmp_path / "e.jsonl"
        traces = [list(range(10))]  # 2 complete chunks of 4, plus 2 tail tokens
        with open(path, "w", encoding="utf-8") as handle:
            write_lookup_events(
                requests=[
                    TraceRequest(request_id="r0", timestamp=0.0, token_ids=traces[0])
                ],
                token_ids_per_request=traces,
                hasher=TokenHasher(chunk_size=CHUNK),
                model_name="m",
                shape=kv_shape(CHUNK, 1, 1, 1),
                dtype="bfloat16",
                handle=handle,
            )
        event = json.loads(path.read_text().strip())
        assert event["seq_len"] == 10
        assert len(event["chunk_hashes"]) == 2


class TestLMCacheBehavioursAreReproduced:
    """The assertions that make output from this tool trustworthy."""

    def test_identical_repeat_is_a_complete_hit(self, tmp_path):
        tokens = list(range(16))  # exactly 4 chunks, no tail
        rates = hit_rates(tmp_path, [tokens, tokens])
        assert rates[0] == pytest.approx(0.0)
        assert rates[1] == pytest.approx(1.0)

    def test_changing_an_early_token_invalidates_everything_after_it(self, tmp_path):
        """Hashing is rolling, so a hit requires an exact prefix match.

        A content-only hash would report this as a near-complete hit because
        15 of 16 tokens are unchanged. Rolling hashing makes it a total miss,
        and that difference is the whole reason this tool calls LMCache's
        hasher instead of computing its own.
        """
        tokens = list(range(16))
        altered = [999] + tokens[1:]
        rates = hit_rates(tmp_path, [tokens, altered])
        assert rates[1] == pytest.approx(0.0)

    def test_changing_a_late_token_preserves_the_earlier_prefix(self, tmp_path):
        """The mirror image: only chunks at and after the change are lost."""
        tokens = list(range(16))
        altered = tokens[:12] + [999, 1000, 1001, 1002]
        rates = hit_rates(tmp_path, [tokens, altered])
        # First three of four chunks still match.
        assert rates[1] == pytest.approx(12 / 16)

    def test_trailing_partial_chunk_never_hits(self, tmp_path):
        """A repeat of a prompt with a tail cannot reach a 100% hit rate.

        Only complete chunks are cached, so the tail is permanently prefill.
        This puts a floor on the uncached fraction that is largest for short
        prompts -- the reason a short prompt sits in the partial-hit regime
        structurally, regardless of workload.
        """
        tokens = list(range(18))  # 4 chunks of 4, plus a 2-token tail
        rates = hit_rates(tmp_path, [tokens, tokens])
        assert rates[1] == pytest.approx(16 / 18)
        assert rates[1] < 1.0

    def test_growing_conversation_hits_its_shared_prefix(self, tmp_path):
        """The multi-turn chat shape: each turn extends the previous prompt.

        Turn N should hit on everything turn N-1 stored, which is what makes
        chat the near-complete-hit regime where pipelining has least to offer.
        """
        turn1 = list(range(8))
        turn2 = list(range(16))
        turn3 = list(range(24))
        rates = hit_rates(tmp_path, [turn1, turn2, turn3])
        assert rates[0] == pytest.approx(0.0)
        assert rates[1] == pytest.approx(8 / 16)
        assert rates[2] == pytest.approx(16 / 24)

    def test_shared_document_prefix_with_distinct_suffixes(self, tmp_path):
        """The RAG shape: a common prefix, then a per-request question."""
        document = list(range(16))
        q1 = document + [100, 101, 102, 103]
        q2 = document + [200, 201, 202, 203]
        rates = hit_rates(tmp_path, [q1, q2])
        # q2 reuses the document but not the question.
        assert rates[1] == pytest.approx(16 / 20)

    def test_unrelated_traffic_never_hits(self, tmp_path):
        """The negative control."""
        rates = hit_rates(tmp_path, [list(range(0, 16)), list(range(100, 116))])
        assert all(r == pytest.approx(0.0) for r in rates)
