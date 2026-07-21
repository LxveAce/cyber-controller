"""Wardrive CSV upload (src/core/wardrive_upload.py) — WiGLE + WDG Wars. Pure multipart/parse logic + a
monkeypatched network call; no test ever touches wigle.net or wdgwars.pl."""
from __future__ import annotations

import io
import urllib.error

import pytest

from src.core import wardrive_upload as W


def _resp(body: bytes, status: int = 200):
    class _R:
        def __init__(self):
            self.status = status

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R()


def test_is_configured():
    assert not W.is_configured(None) and not W.is_configured("") and not W.is_configured("   ")
    assert W.is_configured("QVBJTkFNRTpUT0tFTg==")


def test_provider_choices_and_lookup():
    keys = [k for k, _ in W.provider_choices()]
    assert "wigle" in keys and "wdgwars" in keys
    assert W.get_provider("wigle").url.startswith("https://api.wigle.net/")
    assert W.get_provider("wdgwars").url == "https://wdgwars.pl/api/upload-csv"
    assert W.get_provider("nonsense").key == W.DEFAULT_PROVIDER   # unknown -> default


def test_build_multipart_carries_the_file_and_extra_fields():
    ct, body = W.build_multipart("drive.csv", b"WigleWifi-1.6\nrow1\n", {"donate": "on"})
    assert ct.startswith("multipart/form-data; boundary=")
    text = body.decode("utf-8")
    assert 'name="file"; filename="drive.csv"' in text
    assert "Content-Type: text/csv" in text and "WigleWifi-1.6" in text
    assert 'name="donate"' in text and "\r\non\r\n" in text
    assert text.rstrip().endswith("--")            # closing boundary


def test_parse_wigle_success_and_failure():
    assert W.parse_wigle_response('{"success": true, "results": [{"transid": "20260721-00042"}]}')["transid"] \
        == "20260721-00042"
    assert W.parse_wigle_response('{"success": true, "transid": "20260721-1"}')["transid"] == "20260721-1"
    with pytest.raises(W.UploadError, match="too many uploads"):
        W.parse_wigle_response('{"success": false, "message": "too many uploads today"}')
    with pytest.raises(W.UploadError):
        W.parse_wigle_response("<html>rate limited</html>")


def test_parse_wdgwars_lenient_on_2xx_but_honours_explicit_failure():
    # A 2xx body: plain text or a happy JSON = success; an explicit failure JSON raises.
    assert W.parse_wdgwars_response("OK")["message"] == "OK"
    assert W.parse_wdgwars_response('{"id": "sess-9", "message": "stored"}')["transid"] == "sess-9"
    assert W.parse_wdgwars_response("")["message"] == "uploaded"
    with pytest.raises(W.UploadError, match="quota"):
        W.parse_wdgwars_response('{"success": false, "message": "quota exceeded"}')


def test_upload_requires_a_token(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\n", encoding="utf-8")
    with pytest.raises(W.UploadError, match="no WiGLE token"):
        W.upload_csv(str(csv), "", provider="wigle")
    with pytest.raises(W.UploadError, match="no WDG Wars token"):
        W.upload_csv(str(csv), "", provider="wdgwars")


def test_upload_rejects_a_token_with_a_line_break(tmp_path, monkeypatch):
    # Capstone fix: a token pasted wrapped across two lines keeps an internal CR/LF that .strip() can't
    # remove; upload_csv must reject it up front with a safe message, and must NOT hit the network.
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def must_not_be_called(req, timeout=None):
        raise AssertionError("network was hit despite a malformed token")

    monkeypatch.setattr(W.urllib.request, "urlopen", must_not_be_called)
    with pytest.raises(W.UploadError, match="malformed"):
        W.upload_csv(str(csv), "Basic AbC\nSeCReT")
    with pytest.raises(W.UploadError, match="malformed"):
        W.upload_csv(str(csv), "tok\ren", provider="wdgwars")


def test_upload_rejects_missing_and_empty_files(tmp_path):
    with pytest.raises(W.UploadError, match="could not read"):
        W.upload_csv(str(tmp_path / "nope.csv"), "tok")
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(W.UploadError, match="empty"):
        W.upload_csv(str(empty), "tok")


def test_wigle_upload_success_uses_basic_auth(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["method"] = req.get_method()
        return _resp(b'{"success": true, "results": [{"transid": "20260721-99"}]}')

    monkeypatch.setattr(W.urllib.request, "urlopen", fake_urlopen)
    out = W.upload_csv(str(csv), "TOKEN123", provider="wigle", donate=True)
    assert out["transid"] == "20260721-99"
    assert seen["url"] == W.get_provider("wigle").url and seen["method"] == "POST"
    assert seen["auth"] == "Basic TOKEN123"


def test_wdgwars_upload_success_uses_x_api_key(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-api-key")   # urllib normalises the header name's case
        seen["auth"] = req.get_header("Authorization")
        return _resp(b'{"id": "wd-2026", "message": "stored"}')

    monkeypatch.setattr(W.urllib.request, "urlopen", fake_urlopen)
    out = W.upload_csv(str(csv), "abcdef0123456789", provider="wdgwars")
    assert out["transid"] == "wd-2026"
    assert seen["url"] == "https://wdgwars.pl/api/upload-csv"
    assert seen["key"] == "abcdef0123456789"        # the key rides X-API-Key, not Basic auth
    assert seen["auth"] is None


def test_upload_offline_is_wrapped(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def boom(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(W.urllib.request, "urlopen", boom)
    with pytest.raises(W.UploadError, match="couldn't reach WDG Wars"):
        W.upload_csv(str(csv), "tok", provider="wdgwars")


def test_upload_401_reports_bad_token(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def unauthorized(req, timeout=None):
        raise urllib.error.HTTPError(W.get_provider("wigle").url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"success": false, "message": "bad auth"}'))

    monkeypatch.setattr(W.urllib.request, "urlopen", unauthorized)
    with pytest.raises(W.UploadError, match="rejected the token"):
        W.upload_csv(str(csv), "badtok")


def test_wigle_error_alias_is_upload_error():
    # Back-compat: the old name still resolves to the same class.
    assert W.WigleError is W.UploadError
