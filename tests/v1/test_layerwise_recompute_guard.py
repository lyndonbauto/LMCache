# SPDX-License-Identifier: Apache-2.0
"""The startup guard against wrong output after a failed layerwise load.

A failed layerwise retrieve is reported to vLLM as load errors. Under
``kv_load_failure_policy="recompute"``, a vLLM without the fix for vllm#49250
reschedules the request from stale state and produces wrong tokens. These
tests cover the decision and the feature detection; vLLM itself is faked.
"""

# Standard
from dataclasses import dataclass, field
from types import ModuleType
import sys

# Third Party
import pytest

# First Party
from lmcache.integration.vllm import vllm_multi_process_adapter as adapter_mod
from lmcache.integration.vllm.vllm_multi_process_adapter import (
    layerwise_recompute_is_unsafe,
    vllm_rewinds_rejected_kv_loads,
)

LAYERWISE_ON = {"lmcache.mp.use_layerwise": "true"}
SCHED_OUTPUT_MODULE = "vllm.v1.core.sched.output"


def _install_sched_output(
    monkeypatch: pytest.MonkeyPatch, cached_request_data: type
) -> None:
    """Make ``vllm.v1.core.sched.output.CachedRequestData`` resolve to a fake."""
    module = ModuleType(SCHED_OUTPUT_MODULE)
    module.CachedRequestData = cached_request_data  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, SCHED_OUTPUT_MODULE, module)


@pytest.mark.parametrize(
    ("extra_config", "policy", "vllm_fixed", "unsafe"),
    [
        (LAYERWISE_ON, "recompute", False, True),
        (LAYERWISE_ON, "recompute", True, False),
        (LAYERWISE_ON, "fail", False, False),
        ({"lmcache.mp.use_layerwise": "false"}, "recompute", False, False),
        (None, "recompute", False, False),
    ],
)
def test_only_layerwise_recompute_on_an_unfixed_vllm_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    extra_config: dict[str, str] | None,
    policy: str,
    vllm_fixed: bool,
    unsafe: bool,
) -> None:
    monkeypatch.setattr(
        adapter_mod, "vllm_rewinds_rejected_kv_loads", lambda: vllm_fixed
    )

    assert layerwise_recompute_is_unsafe(extra_config, policy) is unsafe


def test_a_vllm_with_rewound_req_ids_is_detected_as_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class CachedRequestData:
        req_ids: list[str]
        rewound_req_ids: set[str] = field(default_factory=set)

    _install_sched_output(monkeypatch, CachedRequestData)

    assert vllm_rewinds_rejected_kv_loads() is True


def test_a_vllm_without_rewound_req_ids_is_detected_as_unfixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class CachedRequestData:
        req_ids: list[str]

    _install_sched_output(monkeypatch, CachedRequestData)

    assert vllm_rewinds_rejected_kv_loads() is False


def test_a_non_dataclass_cached_request_data_is_detected_as_unfixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CachedRequestData:
        rewound_req_ids: set[str] = set()

    _install_sched_output(monkeypatch, CachedRequestData)

    assert vllm_rewinds_rejected_kv_loads() is False


def test_without_vllm_the_fix_is_not_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, SCHED_OUTPUT_MODULE, None)

    assert vllm_rewinds_rejected_kv_loads() is False
