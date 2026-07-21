"""Direct upload of a wardrive CSV to a wardriving service — the WS-8 "upload when connected to wifi" path.

The Wardrive tab writes a WigleWifi-1.6 CSV; this module hands it straight to a wardriving upload service so
the operator never has to open a browser. It's opt-in and credential-gated: nothing is sent until a per-service
token is set in Settings, and each upload only ever targets that service's one fixed HTTPS endpoint below —
there is no user-supplied URL, so it can't be pointed anywhere else.

Two services are supported, both taking the same WigleWifi CSV the Wardrive tab already produces:
- **WiGLE** (wigle.net): the "Encoded for use" token as HTTP ``Authorization: Basic``.
- **WDG Wars** (wdgwars.pl): a 64-hex API key in the ``X-API-Key`` header.

The pure pieces (multipart build, response parse, credential check, provider table) are unit-tested with no
network; the one network call is guarded so an offline device reports a clean error instead of raising.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_TIMEOUT = 60.0            # a wardrive CSV can be large; give the upload room but don't hang forever.
_BOUNDARY = "----CyberControllerWardriveBoundary7MA4YWxkTrZu0gW"


class UploadError(Exception):
    """An upload could not be completed (offline, bad credentials, the service rejected the file). The caller
    surfaces the message and leaves the CSV on disk to retry later."""


# Back-compat alias — the module used to be wigle_upload with a WigleError.
WigleError = UploadError


def _looks_ok_2xx(status: int) -> bool:
    return 200 <= status < 300


def _b(s: str) -> bytes:
    return s.encode("utf-8")


def build_multipart(filename: str, filedata: bytes, extra_fields: "Optional[Dict[str, str]]" = None,
                    boundary: str = _BOUNDARY) -> "Tuple[str, bytes]":
    """Build a ``multipart/form-data`` body carrying the CSV under the ``file`` field (plus any extra text
    fields, e.g. ``donate=on`` for WiGLE). Returns ``(content_type, body_bytes)``. Pure + unit-testable."""
    parts: list[bytes] = []
    for name, value in (extra_fields or {}).items():
        parts.append(_b(f"--{boundary}\r\n"))
        parts.append(_b(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'))
        parts.append(_b(f"{value}\r\n"))
    parts.append(_b(f"--{boundary}\r\n"))
    parts.append(_b(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'))
    parts.append(_b("Content-Type: text/csv\r\n\r\n"))
    parts.append(filedata)
    parts.append(_b(f"\r\n--{boundary}--\r\n"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def parse_wigle_response(text: str) -> Dict[str, Any]:
    """WiGLE's JSON reply -> ``{"transid": ..., "message": ...}`` on success, or raise :class:`UploadError`
    with WiGLE's own message on a rejection. WiGLE returns ``{"success": true, "results":[{"transid": ...}]}``
    (older responses put ``transid`` at the top level); a failure carries ``success: false`` + ``message``."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise UploadError(f"WiGLE returned a non-JSON response: {text[:120]!r}") from exc
    if not isinstance(data, dict):
        raise UploadError("WiGLE returned an unexpected response")
    if not data.get("success", False):
        raise UploadError(str(data.get("message") or "WiGLE rejected the upload"))
    transid = ""
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        transid = str(results[0].get("transid") or "")
    transid = transid or str(data.get("transid") or "")
    return {"transid": transid, "message": str(data.get("message") or "uploaded")}


def parse_wdgwars_response(text: str) -> Dict[str, Any]:
    """WDG Wars reply parsing. The endpoint returned HTTP 2xx (checked by the caller), so treat it as
    accepted; extract a message/id if it's JSON, and only raise if the JSON explicitly reports a failure.
    Lenient because the exact success shape isn't pinned — but a JSON ``success:false``/``error`` is honoured."""
    text = (text or "").strip()
    if not text:
        return {"transid": "", "message": "uploaded"}
    try:
        data = json.loads(text)
    except ValueError:
        return {"transid": "", "message": text[:200]}     # plain-text "OK"-style body on a 2xx = success
    if isinstance(data, dict):
        if data.get("success") is False or data.get("error"):
            raise UploadError(str(data.get("message") or data.get("error") or "WDG Wars rejected the upload"))
        tid = str(data.get("id") or data.get("transid") or data.get("session") or "")
        return {"transid": tid, "message": str(data.get("message") or data.get("status") or "uploaded")}
    return {"transid": "", "message": "uploaded"}


class Provider:
    """A wardriving upload target: a fixed HTTPS endpoint, how to attach the credential, and how to read the
    reply. Code-controlled (the operator picks a key, never a URL), so it is not an SSRF surface."""

    def __init__(self, key: str, label: str, url: str,
                 auth: "Callable[[str], Tuple[str, str]]", parse: "Callable[[str], Dict[str, Any]]",
                 extra_fields: "Optional[Dict[str, str]]" = None) -> None:
        self.key = key
        self.label = label
        self.url = url
        self._auth = auth
        self._parse = parse
        self.extra_fields = extra_fields or {}

    def auth_header(self, token: str) -> "Tuple[str, str]":
        return self._auth(token)

    def parse(self, text: str) -> Dict[str, Any]:
        return self._parse(text)


PROVIDERS: "Dict[str, Provider]" = {
    "wigle": Provider(
        "wigle", "WiGLE", "https://api.wigle.net/api/v2/file/upload",
        auth=lambda t: ("Authorization", f"Basic {t}"), parse=parse_wigle_response),
    "wdgwars": Provider(
        "wdgwars", "WDG Wars", "https://wdgwars.pl/api/upload-csv",
        auth=lambda t: ("X-API-Key", t), parse=parse_wdgwars_response),
}
DEFAULT_PROVIDER = "wigle"


def get_provider(key: str) -> Provider:
    return PROVIDERS.get((key or "").strip().lower(), PROVIDERS[DEFAULT_PROVIDER])


def provider_choices() -> "list[Tuple[str, str]]":
    """(key, label) pairs for the UI provider selector, in a stable order."""
    return [(k, p.label) for k, p in PROVIDERS.items()]


def is_configured(token: "str | None") -> bool:
    """True when a non-empty token is set — the gate the UI checks before offering an upload."""
    return bool((token or "").strip())


def upload_csv(csv_path: str, token: str, *, provider: str = DEFAULT_PROVIDER, donate: bool = False,
               timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Upload the WigleWifi CSV at *csv_path* to the chosen *provider* using *token*. Returns the parsed
    result (a ``transid``/``message``); raises :class:`UploadError` on any failure (no token, unreadable
    file, offline, non-2xx, rejection). Never raises anything else."""
    prov = get_provider(provider)
    token = (token or "").strip()
    if not token:
        raise UploadError(f"no {prov.label} token set — add it in Settings ▸ Wardrive uploads first")
    # Reject a token with embedded control chars (a CR/LF survives .strip() when a key is pasted wrapped
    # across two lines). Left unchecked it reaches an auth header, where http.client raises a ValueError
    # whose message contains the whole token verbatim — which would then escape into the (screenshot-able)
    # log. Fail here with a safe message instead of leaking the credential.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in token):
        raise UploadError(f"that {prov.label} token looks malformed (it has a line break or control "
                          f"character) — re-copy the value as a single line")
    if not prov.url.lower().startswith("https://"):   # defensive: never upload over plaintext
        raise UploadError("refusing to upload over a non-HTTPS endpoint")
    try:
        with open(csv_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise UploadError(f"could not read {os.path.basename(csv_path)}: {exc}") from exc
    if not data:
        raise UploadError("the wardrive CSV is empty — capture some APs first")

    fields = dict(prov.extra_fields)
    if donate and prov.key == "wigle":
        fields["donate"] = "on"
    content_type, body = build_multipart(os.path.basename(csv_path), data, fields)
    hdr_name, hdr_val = prov.auth_header(token)
    req = urllib.request.Request(
        prov.url, data=body, method="POST",
        headers={
            hdr_name: hdr_val,
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "CyberController-Wardrive/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https endpoint
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise UploadError(f"{prov.label} rejected the token ({exc.code}) — check the token in Settings") from exc
        try:
            return prov.parse(body_txt)   # a JSON error body -> its message
        except UploadError:
            raise
        except Exception as exc2:  # noqa: BLE001
            raise UploadError(f"{prov.label} upload failed (HTTP {exc.code})") from exc2
    except (urllib.error.URLError, OSError) as exc:
        raise UploadError(f"couldn't reach {prov.label} (offline?): {exc}") from exc
    if not _looks_ok_2xx(status):
        raise UploadError(f"{prov.label} upload failed (HTTP {status})")
    return prov.parse(raw)
