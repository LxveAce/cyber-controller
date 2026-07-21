"""Direct WiGLE upload of a wardrive CSV — the WS-8 "upload when connected to wifi" path.

The Wardrive tab already writes a WiGLE-format CSV (``WigleWifi-1.6``); this module hands that file straight
to wigle.net's documented upload endpoint so the operator never has to open a browser. It's opt-in and
credential-gated: nothing is sent until the operator pastes their WiGLE **"Encoded for use"** token (the
base64 ``apiName:apiToken`` WiGLE shows on the account page) into Settings, and the upload only ever targets
the one fixed HTTPS endpoint below — there is no user-supplied URL, so it can't be pointed anywhere else.

The pure pieces (multipart build, response parse, credential check) are unit-tested with no network; the one
network call is guarded so an offline device just reports "couldn't reach WiGLE" instead of raising.

A second wardrive service the owner referred to as "WDG wars" is intentionally NOT wired yet — the exact
service is unconfirmed; :func:`upload_csv` is written provider-neutral enough that a second uploader slots in
beside it once the owner names it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

# The one endpoint we ever POST to (documented WiGLE v2 file upload). A code constant, HTTPS, fixed host —
# there is no user-controlled URL, so this is not an SSRF surface.
WIGLE_UPLOAD_URL = "https://api.wigle.net/api/v2/file/upload"
WIGLE_HOST = "api.wigle.net"

DEFAULT_TIMEOUT = 60.0            # a wardrive CSV can be large; give the upload room but don't hang forever.
_BOUNDARY = "----CyberControllerWigleBoundary7MA4YWxkTrZu0gW"


class WigleError(Exception):
    """An upload could not be completed (offline, bad credentials, WiGLE rejected the file). The caller
    surfaces the message and leaves the CSV on disk to retry later."""


def is_configured(token: "str | None") -> bool:
    """True when a non-empty WiGLE token is set — the gate the UI checks before offering an upload."""
    return bool((token or "").strip())


def _b(s: str) -> bytes:
    return s.encode("utf-8")


def build_multipart(filename: str, filedata: bytes, extra_fields: "Optional[Dict[str, str]]" = None,
                    boundary: str = _BOUNDARY) -> "Tuple[str, bytes]":
    """Build a ``multipart/form-data`` body carrying the CSV under the ``file`` field (plus any extra text
    fields, e.g. ``donate=on``). Returns ``(content_type, body_bytes)``. Pure + unit-testable."""
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


def parse_upload_response(text: str) -> Dict[str, Any]:
    """Turn WiGLE's JSON reply into ``{"transid": ..., "message": ...}`` on success, or raise
    :class:`WigleError` with WiGLE's own message on a rejection. Pure + unit-testable.

    WiGLE returns ``{"success": true, "results":[{"transid": "..."}]}`` (older responses put ``transid`` at
    the top level); a failure carries ``success: false`` + a ``message``.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WigleError(f"WiGLE returned a non-JSON response: {text[:120]!r}") from exc
    if not isinstance(data, dict):
        raise WigleError("WiGLE returned an unexpected response")
    if not data.get("success", False):
        raise WigleError(str(data.get("message") or "WiGLE rejected the upload"))
    transid = ""
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        transid = str(results[0].get("transid") or "")
    transid = transid or str(data.get("transid") or "")
    return {"transid": transid, "message": str(data.get("message") or "uploaded")}


def upload_csv(csv_path: str, token: str, *, donate: bool = False,
               timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Upload the WiGLE CSV at *csv_path* to wigle.net using the "Encoded for use" *token* (an
    ``Authorization: Basic`` value). Returns the parsed result (``transid``); raises :class:`WigleError` on
    any failure (no token, unreadable file, offline, non-200, WiGLE rejection). Never raises anything else."""
    token = (token or "").strip()
    if not token:
        raise WigleError("no WiGLE token set — paste your 'Encoded for use' token in Settings first")
    if not WIGLE_UPLOAD_URL.lower().startswith("https://"):   # defensive: never upload over plaintext
        raise WigleError("refusing to upload over a non-HTTPS endpoint")
    import os
    try:
        with open(csv_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise WigleError(f"could not read {os.path.basename(csv_path)}: {exc}") from exc
    if not data:
        raise WigleError("the wardrive CSV is empty — capture some APs first")

    fields = {"donate": "on"} if donate else {}
    content_type, body = build_multipart(os.path.basename(csv_path), data, fields)
    req = urllib.request.Request(
        WIGLE_UPLOAD_URL, data=body, method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "CyberController-Wardrive/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https endpoint
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # WiGLE puts a helpful JSON message in the error body (e.g. 401 bad token) — surface it.
        body_txt = ""
        try:
            body_txt = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 401:
            raise WigleError("WiGLE rejected the token (401) — check your 'Encoded for use' token") from exc
        try:
            return parse_upload_response(body_txt)   # a JSON error body -> its message
        except WigleError:
            raise
        except Exception as exc2:  # noqa: BLE001
            raise WigleError(f"WiGLE upload failed (HTTP {exc.code})") from exc2
    except (urllib.error.URLError, OSError) as exc:
        raise WigleError(f"couldn't reach WiGLE (offline?): {exc}") from exc
    return parse_upload_response(raw)
