"""Allowlist contents and matching rules."""

from __future__ import annotations

import pytest

from custom_components.silverblaze.api import (
    SILVERBLAZE_ENDPOINTS,
    Allowlist,
    EndpointNotAllowed,
    SilverblazeError,
    build_allowlist,
)

HOST = "acewebsite.silverblaze.com"
LOGIN = [("tools.example.test", "/Login"), ("auth.example.test", "/connect/authorize")]

FORBIDDEN = [
    f"https://{HOST}{path}"
    for path in (
        "/api/WebToken/ConfirmWebToken",
        "/api/Page/PostToken",
        "/api/CustomerInfo/Get",
        "/api/CustomerInfo/GetAccountPremise",
        "/api/Profile/GetProfile",
        "/api/Profile/PostProfile",
        "/api/GreenButtonConnect/UpdateCustomerSubscription",
        "/api/AutoPay/Get",
        "/api/Pay/Post",
        "/api/PaymentMethod/Get",
        "/api/Budget/Get",
        "/PageAnything",
        "/Page/",
        "/api/ConsumptionV3/DataExtra",
    )
] + [
    f"http://{HOST}/Page",
    "https://evil.example.test/Page",
    f"https://{HOST}.example.test/Page",
]


def test_silverblaze_endpoints_exact():
    assert {
        (HOST, "/Page"),
        (HOST, "/api/Page/GetPageParameters"),
        (HOST, "/api/DataDownload/Meta"),
        (HOST, "/api/ConsumptionV3/Init"),
        (HOST, "/api/ConsumptionV3/Data"),
    } == SILVERBLAZE_ENDPOINTS


def test_build_allowlist_is_union():
    al = build_allowlist(LOGIN)
    assert al.endpoints == SILVERBLAZE_ENDPOINTS | set(LOGIN)


@pytest.mark.parametrize("url", FORBIDDEN)
def test_forbidden_urls_raise(url):
    with pytest.raises(EndpointNotAllowed):
        build_allowlist(LOGIN).check("GET", url)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/Page",
        f"https://{HOST.upper()}/Page",
        f"https://{HOST}:443/Page",
        f"https://{HOST}/api/ConsumptionV3/Data?paging=true",
        "https://tools.example.test/Login",
        "https://auth.example.test/connect/authorize?x=1#frag",
    ],
)
def test_allowed_urls_pass(url):
    build_allowlist(LOGIN).check("GET", url)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}:8443/Page",
        f"https://user@{HOST}/Page",
        f"https://{HOST}./Page",
        f"https://{HOST}/page",
        f"https://{HOST}/Page/../Page/x",
        "not a url",
        "https://[::1/Page",
    ],
)
def test_odd_urls_rejected(url):
    with pytest.raises(EndpointNotAllowed):
        build_allowlist([]).check("GET", url)


def test_message_has_host_path_only():
    with pytest.raises(EndpointNotAllowed) as ei:
        Allowlist(frozenset()).check("GET", f"https://{HOST}/Page?enc=SECRETVALUE#x")
    assert f"{HOST}/Page" in str(ei.value)
    assert "SECRETVALUE" not in str(ei.value)
    assert "?" not in str(ei.value)


def test_endpoint_not_allowed_not_a_silverblaze_error():
    assert not issubclass(EndpointNotAllowed, SilverblazeError)
    assert SilverblazeError not in EndpointNotAllowed.__mro__
