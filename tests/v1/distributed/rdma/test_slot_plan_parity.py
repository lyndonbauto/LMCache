# SPDX-License-Identifier: Apache-2.0
"""The C++ and Python slot planners must agree, slot for slot.

The slot arithmetic is implemented twice on purpose. ``SlotPlanner`` in
``csrc/storage_backends/aerospike/`` drives the production fetch path, and
``FetchPlanner`` in ``lmcache/v1/layerwise/`` lets Track C plan and test
without a native build (acceptance criterion C10). Each has its own tests.

Neither can catch the two drifting apart, and drift is not a crash. Both
plans would still tile the payload; they would simply disagree about which
bytes a given slot carries, so a slot would name a record the write side
never produced -- or worse, name a real record and land it at the offset the
other side meant for a different piece. That reads back as a plausible
tensor with no error anywhere.

So both read ``fixtures/slot_plans.txt`` and this compares the results.
"""

# Standard
from collections.abc import Callable
from pathlib import Path
import subprocess

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    ChunkPlacement,
    FetchPlanner,
    KernelGroupGeometry,
    LayerFetchPlan,
    ModelLayout,
    PlanRequest,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "slot_plans.txt"

#: The parity check is about geometry, so the Python planner is given a
#: digest source that is valid and otherwise uninteresting. Digests are
#: joined onto the plan after planning on the C++ side, so they are not
#: something the two could disagree about.
_PLACEHOLDER_DIGEST = b"\x01" * 20


class _ConstantDigests:
    """A digest source that names one record for every slot."""

    def digest_for(self, chunk_id: int, layer_id: int, plane: int, piece: int) -> bytes:
        """Return a fixed digest, since parity does not depend on digests."""
        del chunk_id, layer_id, plane, piece
        return _PLACEHOLDER_DIGEST


class _Case:
    """One fixture case: a model layout, a set of placements, and the caps."""

    def __init__(self, name: str) -> None:
        """Start an empty case.

        Args:
            name: The case's name, used in failure messages.
        """
        self.name = name
        self.kernel_groups: dict[int, list[KernelGroupGeometry]] = {}
        self.placements: list[ChunkPlacement] = []
        self.max_record_bytes = 0

    def plan(self) -> LayerFetchPlan:
        """Build this case's plan with the Python planner.

        Returns:
            The plan the Python planner produces for this case.
        """
        layout = ModelLayout(
            {group: self.kernel_groups[group] for group in sorted(self.kernel_groups)}
        )
        request = PlanRequest(
            placements=tuple(self.placements),
            node_names=("node-a",),
            max_record_bytes=self.max_record_bytes,
        )
        return FetchPlanner(layout).plan(request, _ConstantDigests())

    def render(self) -> list[str]:
        """Render this case's Python plan in the harness's output form.

        Returns:
            One ``case`` line followed by one ``slot`` line per slot.
        """
        lines = [f"case {self.name}"]
        for index, slot in enumerate(self.plan().slots):
            lines.append(
                f"slot {index} {slot.layer_id} {slot.chunk_id} "
                f"{slot.offset} {slot.length}"
            )
        return lines


def _parse_fixture(text: str) -> list[_Case]:
    """Read the shared fixture into cases.

    Deliberately a second parser rather than a shared one: the point of the
    fixture is that both sides read it independently, so a parser only one
    side used would reintroduce the coupling this test exists to avoid.

    Args:
        text: Contents of the fixture file.

    Returns:
        Every case in the fixture, in file order.

    Raises:
        ValueError: If a directive is unknown or appears outside a case, or
            the file ends mid-case.
    """
    cases: list[_Case] = []
    current: _Case | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        directive, values = fields[0], fields[1:]

        if directive == "case":
            current = _Case(values[0])
            continue
        if current is None:
            raise ValueError(f"directive {directive!r} outside a case at line {number}")

        if directive == "kernel":
            group_id, kv_size, num_slots, hidden_dim, element_size = (
                int(value) for value in values[:5]
            )
            current.kernel_groups.setdefault(group_id, []).append(
                KernelGroupGeometry(
                    layer_ids=tuple(int(value) for value in values[5:]),
                    kv_planes=kv_size,
                    plane_bytes=num_slots * hidden_dim * element_size,
                )
            )
        elif directive == "place":
            chunk_id, group_id, dest_offset = (int(value) for value in values)
            current.placements.append(
                ChunkPlacement(
                    chunk_id=chunk_id,
                    object_group_id=group_id,
                    node_index=0,
                    dest_offset=dest_offset,
                )
            )
        elif directive == "caps":
            # The second cap is the device's maximum RDMA write, which the
            # transport enforces rather than the planner.
            current.max_record_bytes = int(values[0])
        elif directive == "end":
            cases.append(current)
            current = None
        else:
            raise ValueError(f"unknown directive {directive!r} at line {number}")

    if current is not None:
        raise ValueError(f"fixture ended inside case {current.name!r}")
    return cases


def _cases_from_output(lines: list[str]) -> dict[str, list[str]]:
    """Group rendered lines by the case they belong to.

    Args:
        lines: Rendered output, ``case`` lines each followed by ``slot`` lines.

    Returns:
        A mapping from case name to that case's lines, including its ``case``
        line.
    """
    grouped: dict[str, list[str]] = {}
    name = ""
    for line in lines:
        if line.startswith("case "):
            name = line.split(maxsplit=1)[1]
            grouped[name] = []
        grouped[name].append(line)
    return grouped


def test_the_fixture_exercises_more_than_one_geometry() -> None:
    """A fixture that lost its hard cases would pass parity vacuously.

    Parity is only as strong as the cases both sides run, and this test is
    the only thing standing between "the planners agree" and "the planners
    agree about one easy shape". So the properties that make the fixture
    worth running are asserted rather than assumed.
    """
    cases = _parse_fixture(_FIXTURE.read_text())
    assert len(cases) >= 8

    assert any(
        len(groups) > 1 for case in cases for groups in case.kernel_groups.values()
    ), (
        "no case puts two kernel groups in one object group, so hybrid "
        "striding is untested"
    )
    assert any(len(case.kernel_groups) > 1 for case in cases), (
        "no case spans two object groups"
    )
    assert any(
        group.kv_planes == 1
        for case in cases
        for groups in case.kernel_groups.values()
        for group in groups
    ), "no case covers the single-plane MLA layout"
    assert any(
        group.plane_bytes % case.max_record_bytes != 0
        for case in cases
        for groups in case.kernel_groups.values()
        for group in groups
    ), "no case has a plane that the record cap does not divide evenly"


def test_every_fixture_case_plans_identically_in_both_languages(
    logic_harness_with_fixture: Callable[[str, Path], str],
) -> None:
    """The two planners produce the same slots, in the same order.

    Order is compared, not just the set of slots, because a slot's index is
    its position: the RDMA immediate carries that position and nothing else,
    so two planners that agree on the bytes but disagree on the ordering
    would still misattribute every arrival.

    Args:
        logic_harness_with_fixture: Fixture that builds and runs a harness
            against a fixture file.
    """
    native = _cases_from_output(
        logic_harness_with_fixture("slot_plan_dump", _FIXTURE).splitlines()
    )
    cases = _parse_fixture(_FIXTURE.read_text())

    assert [case.name for case in cases] == list(native), (
        "the harness and the fixture parser disagree about which cases exist"
    )
    for case in cases:
        assert case.render() == native[case.name], (
            f"case {case.name}: the Python and C++ planners disagree"
        )


def test_the_harness_rejects_a_fixture_it_cannot_parse(
    logic_harness_build_dir: Path, tmp_path: Path
) -> None:
    """A malformed fixture fails rather than dumping nothing.

    A harness that printed no cases for an unreadable fixture would make the
    parity test pass by comparing two empty results.

    Args:
        logic_harness_build_dir: Directory holding the built harnesses.
        tmp_path: Pytest-provided scratch directory.
    """
    bad = tmp_path / "bad.txt"
    bad.write_text("kernel 0 2 8 64 2 0 1\n")

    result = subprocess.run(
        [str(logic_harness_build_dir / "slot_plan_dump"), str(bad)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "outside a case" in result.stderr


def test_a_missing_fixture_is_an_error_not_an_empty_dump(
    logic_harness_build_dir: Path, tmp_path: Path
) -> None:
    """A fixture path that does not resolve fails loudly.

    Args:
        logic_harness_build_dir: Directory holding the built harnesses.
        tmp_path: Pytest-provided scratch directory.
    """
    result = subprocess.run(
        [str(logic_harness_build_dir / "slot_plan_dump"), str(tmp_path / "nope.txt")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "directive",
    ["kernel 0 0 8 64 2 0 1", "kernel 0 2 8 64 2 0 0"],
)
def test_an_ambiguous_layout_is_refused_by_both_planners(
    logic_harness_build_dir: Path, tmp_path: Path, directive: str
) -> None:
    """Both sides reject the same unplannable layouts.

    Agreeing on what is valid matters as much as agreeing on the slots: a
    layout one side plans and the other refuses is a fetch that works in
    tests and fails in production, or the reverse.

    Args:
        logic_harness_build_dir: Directory holding the built harnesses.
        tmp_path: Pytest-provided scratch directory.
        directive: A kernel directive describing an unplannable layout.
    """
    fixture = tmp_path / "ambiguous.txt"
    fixture.write_text(f"case bad\n{directive}\nplace 0 0 0\ncaps 4096 1048576\nend\n")

    result = subprocess.run(
        [str(logic_harness_build_dir / "slot_plan_dump"), str(fixture)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0, "the C++ planner accepted an ambiguous layout"

    with pytest.raises(ValueError):
        _parse_fixture(fixture.read_text())[0].plan()
