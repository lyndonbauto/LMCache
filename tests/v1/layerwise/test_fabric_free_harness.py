# SPDX-License-Identifier: Apache-2.0
"""The fabric-free native client reports its node like the production one.

``RdmaWindowPlacer`` is built with the name ``pipelined_fetch_node_name()``
returns, so the harness must give the same answer the native driver does:
the one registered node, and a refusal for any other count.
"""

# Third Party
import pytest

# Local
from .aerospike_harness import fabric_free_connector
from .conftest import TEST_NODE_NAMES


def test_a_single_node_harness_reports_that_node() -> None:
    """With one registered node, that node is the answer."""
    assert fabric_free_connector().pipelined_fetch_node_name() == TEST_NODE_NAMES[0]


def test_a_two_node_harness_has_no_single_node() -> None:
    """Pipelined fetches run on single-node clusters only."""
    connector = fabric_free_connector(node_names=("node-a", "node-b"))
    with pytest.raises(RuntimeError, match="one registered node"):
        connector.pipelined_fetch_node_name()
