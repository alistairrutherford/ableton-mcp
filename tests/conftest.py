import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The Remote Script's server and commands modules, imported directly (top level), because the
# package __init__ needs Live's _Framework.
sys.path.insert(0, os.path.join(ROOT, "RemoteScript", "ClaudeLiveBridge"))

from livebridge.client import BRIDGE_NAME, LiveClient  # noqa: E402

_REPORTS = []


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs Ableton Live running with the %s control surface" % BRIDGE_NAME)


@pytest.fixture(scope="session")
def live():
    client = LiveClient(timeout=10.0)
    try:
        client.connect()
        client.call("ping")
    except OSError as e:
        pytest.skip("%s not reachable on %s:%d (%s). Start Live and select the %s control surface."
                    % (BRIDGE_NAME, client.host, client.port, e, BRIDGE_NAME))
    yield client
    client.close()


@pytest.fixture
def report():
    """Collect text blocks that are printed at the end of the run, whatever -s/-q says."""
    return _REPORTS.append


def pytest_terminal_summary(terminalreporter):
    if _REPORTS:
        terminalreporter.section("Live test report")
        for block in _REPORTS:
            terminalreporter.write_line(block)
