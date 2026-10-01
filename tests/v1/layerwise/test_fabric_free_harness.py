# SPDX-License-Identifier: Apache-2.0
"""The fabric-free native client reports its node like the production one.

``RdmaWindowPlacer`` is built with the name ``pipelined_fetch_node_name()``
returns, so the harness must give the same answer the native driver does:
the first node of the cluster, whatever its size, and a refusal for none.
"""

# Third Party
import pytest

# Local
from .aerospike_harness import fabric_free_connector
from .conftest import TEST_NODE_NAMES


def test_a_single_node_harness_reports_that_node() -> None:
    """With one node, that node is the answer."""
    assert fabric_free_connector().pipelined_fetch_node_name() == TEST_NODE_NAMES[0]


def test_a_two_node_harness_reports_its_first_node() -> None:
    """Sink rows are routed by partition, so a larger cluster is served too."""
    connector = fabric_free_connector(node_names=("node-a", "node-b"))
    assert connector.pipelined_fetch_node_name() == "node-a"


def test_an_empty_harness_has_no_node() -> None:
    """A cluster with no nodes has nothing to name."""
    connector = fabric_free_connector(node_names=())
    with pytest.raises(RuntimeError, match="no nodes"):
        connector.pipelined_fetch_node_name()
