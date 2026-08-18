"""Pytest configuration and fixtures for GCal Glance."""

from collections.abc import Generator

import pytest
import respx

from app import BASE


@pytest.fixture
def mock_busy_bar_api() -> Generator[respx.MockRouter, None, None]:
    """Mock router scoped to the BUSY Bar BASE URL (no real API calls)."""
    with respx.mock(base_url=BASE, assert_all_mocked=True) as respx_mock:
        yield respx_mock
