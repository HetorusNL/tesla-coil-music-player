#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import os
from collections.abc import Iterator

import pytest

from tests.sparks import LoggingController


def pytest_addoption(parser: pytest.Parser):
    parser.addoption(
        "--port",
        default=os.environ.get("TESLA_COIL_PORT"),
        help="serial port of the controller running the test firmware, for the hardware tests "
        "(default: $TESLA_COIL_PORT)",
    )


@pytest.fixture(scope="session")
def port(request: pytest.FixtureRequest) -> str:
    port = request.config.getoption("--port")
    if not port:
        pytest.skip("needs --port: the controller with the test firmware (pio run -e nucleo_spark_test -t upload)")
    return port


# per module, so the tests of the player can open the port themselves
@pytest.fixture(scope="module")
def controller(port: str) -> Iterator[LoggingController]:
    controller = LoggingController(port)
    controller.hello()
    yield controller
    controller.stop()
    controller.close()


@pytest.fixture
def coil(controller: LoggingController) -> LoggingController:
    """The controller, silent and with the default limits."""
    controller.stop()
    controller.set_limits()
    return controller
