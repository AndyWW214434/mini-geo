"""A typo in CORS_ORIGINS must not stop the API from booting."""

import pytest

from geo.config import Settings

WEB = "https://geo-web-x.onrender.com"


@pytest.mark.parametrize("raw", [
    f'["{WEB}"]', WEB, f"{WEB}/", f"['{WEB}']", f'"["{WEB}"]"', f" {WEB} ", f"[{WEB}]",
])
def test_cors_origins_forms(monkeypatch, raw):
    monkeypatch.setenv("CORS_ORIGINS", raw)
    assert Settings().cors_origins == [WEB]


def test_cors_origins_several_and_empty(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f"{WEB}, http://localhost:3000")
    assert Settings().cors_origins == [WEB, "http://localhost:3000"]
    monkeypatch.setenv("CORS_ORIGINS", "")
    assert Settings().cors_origins == ["http://localhost:3000"]
