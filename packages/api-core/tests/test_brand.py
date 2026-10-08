"""The brand seam: Databench unless a product registers its brand, with the
deployment's settings able to rename the product or the sender."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from alkera_core import brand
from alkera_core.brand import DATABENCH, Brand
from alkera_core.config import settings
from alkera_core.extensions import ExtensionError, ExtensionPoint

ACME = Brand(
    key="acme",
    product_name="Acme",
    sender_name="Acme Mail",
    attribution="Acme",
    terminal_wordmark=("ACME",),
    terminal_mark=("A",),
)


@pytest.fixture
def point(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[Brand]:
    """A fresh BRAND point, as an open process sees it before anything registers."""
    fresh: ExtensionPoint[Brand] = ExtensionPoint("alkera.brand")
    monkeypatch.setattr(brand, "BRAND", fresh)
    monkeypatch.setattr(settings, "brand_product_name", None)
    monkeypatch.setattr(settings, "smtp_from_name", None)
    monkeypatch.setattr(settings, "email_founders_from_name", None)
    for name in ("brand_support_email", "brand_sales_email", "email_founders_from"):
        monkeypatch.setattr(settings, name, None)
    return fresh


def test_nothing_registered_is_databench(point: ExtensionPoint[Brand]) -> None:
    assert brand.current_brand() is DATABENCH
    assert brand.product_name() == "Databench"
    assert brand.sender_name() == "Databench"
    assert brand.founders_sender_name() == "Databench"


def test_a_registered_brand_replaces_databench(point: ExtensionPoint[Brand]) -> None:
    point.register(ACME)
    assert brand.product_name() == "Acme"
    assert brand.sender_name() == "Acme Mail"


def test_a_second_brand_is_refused(point: ExtensionPoint[Brand]) -> None:
    point.register(ACME)
    point.register(Brand(**{**_fields(ACME), "key": "other"}))
    with pytest.raises(ExtensionError, match="only one brand"):
        brand.current_brand()


def test_a_brand_registered_after_the_first_read_is_refused(point: ExtensionPoint[Brand]) -> None:
    brand.current_brand()
    with pytest.raises(ExtensionError, match="already read"):
        point.register(ACME)


@pytest.mark.parametrize("configured", ["", None], ids=["empty", "unset"])
def test_an_empty_product_name_setting_defers_to_the_brand(
    point: ExtensionPoint[Brand], monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    monkeypatch.setattr(settings, "brand_product_name", configured)
    point.register(ACME)
    assert brand.product_name() == "Acme"


def test_a_configured_product_name_wins(
    point: ExtensionPoint[Brand], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "brand_product_name", "Acme Data")
    point.register(ACME)
    assert brand.product_name() == "Acme Data"


def test_an_empty_sender_name_sends_a_bare_address(
    point: ExtensionPoint[Brand], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "smtp_from_name", "")
    monkeypatch.setattr(settings, "email_founders_from_name", "Founders")
    assert brand.sender_name() == ""
    assert brand.founders_sender_name() == "Founders"


def test_the_databench_terminal_art_is_rectangular() -> None:
    assert len({len(row) for row in DATABENCH.terminal_wordmark}) == 1
    assert len(DATABENCH.terminal_wordmark) == 5  # one shade per row in the banner
    assert len({len(row) for row in DATABENCH.terminal_mark}) == 1
    assert len(DATABENCH.terminal_mark) == 6  # pairs with the wordmark and the tagline


def _fields(value: Brand) -> dict[str, object]:
    return {name: getattr(value, name) for name in Brand.__dataclass_fields__}


_ADDRESSES = [
    pytest.param(brand.support_email, "brand_support_email", "support_email", id="support"),
    pytest.param(brand.sales_email, "brand_sales_email", "sales_email", id="sales"),
    pytest.param(brand.welcome_email, "email_founders_from", "welcome_email", id="welcome"),
]


@pytest.mark.parametrize(("read", "setting", "brand_field"), _ADDRESSES)
def test_the_open_build_names_no_address(
    point: ExtensionPoint[Brand], read: Callable[[], str | None], setting: str, brand_field: str
) -> None:
    assert read() is None


@pytest.mark.parametrize("configured", ["", None], ids=["empty", "unset"])
@pytest.mark.parametrize(("read", "setting", "brand_field"), _ADDRESSES)
def test_an_unset_address_defers_to_the_brand(
    point: ExtensionPoint[Brand],
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[], str | None],
    setting: str,
    brand_field: str,
    configured: str | None,
) -> None:
    monkeypatch.setattr(settings, setting, configured)
    point.register(Brand(**{**_fields(ACME), brand_field: "brand@acme.example"}))
    assert read() == "brand@acme.example"


@pytest.mark.parametrize(("read", "setting", "brand_field"), _ADDRESSES)
def test_a_configured_address_wins_over_the_brand(
    point: ExtensionPoint[Brand],
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[], str | None],
    setting: str,
    brand_field: str,
) -> None:
    monkeypatch.setattr(settings, setting, "ops@self-hosted.example")
    point.register(Brand(**{**_fields(ACME), brand_field: "brand@acme.example"}))
    assert read() == "ops@self-hosted.example"
