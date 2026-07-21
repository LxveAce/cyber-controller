"""Canonical legal terms (src/core/legal.py) — pure, no Qt.

The load-bearing test is `test_no_specific_cert_asserted_by_default`: with no verifiable certification on
file, the terms must NOT assert a specific FCC certificate (a published false cert claim is a liability, not
a shield); they stand on authorized-use + user-responsibility and disclaim independent certification.
"""
from __future__ import annotations

from src.core import legal


def test_terms_carry_the_defensible_framing():
    md = legal.terms_markdown()
    for phrase in (
        "Terms of Service", "security research", "authorized", "licensed",
        "Authorized targets only", "controlled", "47 U.S.C. §333", "Part 15",
        "AS IS", "Limitation of liability", "WiGLE",
    ):
        assert phrase in md, f"missing framing: {phrase!r}"


def test_no_specific_cert_asserted_by_default():
    # Nothing on file -> disclaim independent certification, never assert a specific one.
    assert legal.FCC_CERT_PENDING == ""
    md = legal.terms_markdown()
    assert "no independent representation" in md
    # No unfinished placeholder or specific certificate leaks into the user-facing text.
    for leak in ("[OWNER", "TODO", "FIXME", "FCC ID:"):
        assert leak not in md, f"placeholder/claim leaked into shipped terms: {leak!r}"


def test_cert_is_cited_only_when_one_is_on_file(monkeypatch):
    monkeypatch.setattr(legal, "FCC_CERT_PENDING", "FCC ID 2ABCD-XYZ (lab authorization #42)")
    md = legal.terms_markdown()
    assert "FCC ID 2ABCD-XYZ" in md
    assert "operated under the following authorization" in md
    # With a real cert cited, the generic disclaimer is replaced (not shown alongside).
    assert "no independent representation" not in md


def test_terms_are_substantial_and_numbered():
    md = legal.terms_markdown()
    assert len(md) > 1500
    for section in ("## 1.", "## 5. Radio", "## 8. Limitation", "## 9. Acceptance"):
        assert section in md, section
