"""The ``personal`` provider kind: named by the catalog, acted through never.

A personal box is its owner's hardware. Every operation the plane could aim at
it (start, stop, terminate, price, enumerate) must refuse with the permanent
"not here", which the meter and the reconcile read as "skip this row" and never
as "gone, terminate it"; a provider that enumerated an empty list instead
would be a licence to terminate.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from alkera_core.compute.availability import SizeQuery
from alkera_core.compute.personal import UNAVAILABLE, PersonalProvider
from alkera_core.compute.provider import (
    COMPUTE_PROVIDER_KINDS,
    PERSONAL,
    ComputeProviderUnavailableError,
    provider_for_kind,
)
from alkera_core.config import settings
from alkera_core.models.compute import ComputeMachineType

_CALLS = [
    pytest.param(
        lambda p: p.create_pod(
            name="x", machine_type=ComputeMachineType(provider=PERSONAL), ssh_public_key=""
        ),
        id="create_pod",
    ),
    pytest.param(lambda p: p.store_credential(uuid4(), {}), id="store_credential"),
    pytest.param(lambda p: p.delete_credential(uuid4()), id="delete_credential"),
    pytest.param(lambda p: p.bind_credential(uuid4(), "m"), id="bind_credential"),
    pytest.param(lambda p: p.pod_status("m"), id="pod_status"),
    pytest.param(lambda p: p.describe("m"), id="describe"),
    pytest.param(lambda p: p.find(uuid4()), id="find"),
    pytest.param(lambda p: p.list_pods(), id="list_pods"),
    pytest.param(lambda p: p.stop("m"), id="stop"),
    pytest.param(lambda p: p.start("m"), id="start"),
    pytest.param(lambda p: p.terminate("m"), id="terminate"),
    pytest.param(lambda p: p.terminate_pod("m"), id="terminate_pod"),
    pytest.param(lambda p: p.catalog_prices(), id="catalog_prices"),
    pytest.param(lambda p: p.catalog_entries(), id="catalog_entries"),
]


def test_the_kind_is_registered_and_in_the_catalog_vocabulary() -> None:
    assert PERSONAL in COMPUTE_PROVIDER_KINDS
    assert isinstance(provider_for_kind(PERSONAL, settings), PersonalProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", _CALLS)
async def test_every_operation_on_a_persons_box_refuses(call: Any) -> None:
    with pytest.raises(ComputeProviderUnavailableError, match=UNAVAILABLE):
        await call(PersonalProvider())


def test_status_mapping_refuses_too() -> None:
    with pytest.raises(ComputeProviderUnavailableError):
        PersonalProvider().normalize_status("running")


@pytest.mark.asyncio
async def test_nothing_can_be_provisioned_and_nothing_is_offered() -> None:
    provider = PersonalProvider()
    assert provider.configured() is False
    answered = await provider.availability([SizeQuery(code="personal", vcpu=1)])
    assert set(answered) == {"personal"}
    assert answered["personal"].detail == UNAVAILABLE
