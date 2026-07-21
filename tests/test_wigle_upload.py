"""WiGLE wardrive upload (src/core/wigle_upload.py). Pure multipart/parse logic + a monkeypatched network
call — no test ever touches wigle.net."""
from __future__ import annotations

import io
import urllib.error

import pytest

from src.core import wigle_upload as W


def test_is_configured():
    assert not W.is_configured(None) and not W.is_configured("") and not W.is_configured("   ")
    assert W.is_configured("QVBJTkFNRTpUT0tFTg==")


def test_build_multipart_carries_the_file_and_extra_fields():
    ct, body = W.build_multipart("drive.csv", b"WigleWifi-1.6\nrow1\n", {"donate": "on"})
    assert ct.startswith("multipart/form-data; boundary=")
    text = body.decode("utf-8")
    assert 'name="file"; filename="drive.csv"' in text
    assert "Content-Type: text/csv" in text
    assert "WigleWifi-1.6" in text
    assert 'name="donate"' in text and "\r\non\r\n" in text
    assert text.rstrip().endswith("--")            # closing boundary


def test_parse_success_with_results_transid():
    out = W.parse_upload_response('{"success": true, "results": [{"transid": "20260721-00042"}]}')
    assert out["transid"] == "20260721-00042"


def test_parse_success_with_top_level_transid():
    out = W.parse_upload_response('{"success": true, "transid": "20260721-1"}')
    assert out["transid"] == "20260721-1"


def test_parse_failure_raises_with_wigle_message():
    with pytest.raises(W.WigleError, match="too many uploads"):
        W.parse_upload_response('{"success": false, "message": "too many uploads today"}')


def test_parse_non_json_raises():
    with pytest.raises(W.WigleError):
        W.parse_upload_response("<html>rate limited</html>")


def test_upload_requires_a_token(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\n", encoding="utf-8")
    with pytest.raises(W.WigleError, match="no WiGLE token"):
        W.upload_csv(str(csv), "")


def test_upload_rejects_a_token_with_a_line_break(tmp_path, monkeypatch):
    # Capstone fix: a token pasted wrapped across two lines keeps an internal CR/LF that .strip() can't
    # remove; left unchecked http.client raises with the token in the message and it leaks to the log.
    # upload_csv must reject it up front with a safe message, and must NOT hit the network.
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def must_not_be_called(req, timeout=None):
        raise AssertionError("network was hit despite a malformed token")

    monkeypatch.setattr(W.urllib.request, "urlopen", must_not_be_called)
    with pytest.raises(W.WigleError, match="malformed"):
        W.upload_csv(str(csv), "Basic AbC\nSeCReT")
    with pytest.raises(W.WigleError, match="malformed"):
        W.upload_csv(str(csv), "tok\ren")


def test_upload_rejects_missing_and_empty_files(tmp_path):
    with pytest.raises(W.WigleError, match="could not read"):
        W.upload_csv(str(tmp_path / "nope.csv"), "tok")
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(W.WigleError, match="empty"):
        W.upload_csv(str(empty), "tok")


def test_upload_success(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")
    seen = {}

    class _Resp:
        def read(self):
            return b'{"success": true, "results": [{"transid": "20260721-99"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(W.urllib.request, "urlopen", fake_urlopen)
    out = W.upload_csv(str(csv), "TOKEN123", donate=True)
    assert out["transid"] == "20260721-99"
    assert seen["url"] == W.WIGLE_UPLOAD_URL and seen["method"] == "POST"
    assert seen["auth"] == "Basic TOKEN123"


def test_upload_offline_is_wrapped(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def boom(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(W.urllib.request, "urlopen", boom)
    with pytest.raises(W.WigleError, match="couldn't reach WiGLE"):
        W.upload_csv(str(csv), "tok")


def test_upload_401_reports_bad_token(tmp_path, monkeypatch):
    csv = tmp_path / "d.csv"
    csv.write_text("WigleWifi-1.6\nrow\n", encoding="utf-8")

    def unauthorized(req, timeout=None):
        raise urllib.error.HTTPError(W.WIGLE_UPLOAD_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"success": false, "message": "bad auth"}'))

    monkeypatch.setattr(W.urllib.request, "urlopen", unauthorized)
    with pytest.raises(W.WigleError, match="rejected the token"):
        W.upload_csv(str(csv), "badtok")
