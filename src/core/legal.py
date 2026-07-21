"""Canonical legal terms (Terms of Service + Terms of Use) surfaced in Help ▸ Terms.

One source of truth so the in-app dialog, the tests, and any future doc export stay in lockstep.

Posture (deliberate, defensible — NOT over-claiming): the copy frames the tool as research/R&D and
authorized-professional use, puts the responsibility for licensing, authorization, and lawful operation on
the operator, and confines interference-capable RF to a controlled-lab / authorized-test context. It does
**not** assert a specific FCC certification identifier for Cyber Controller or any connected device — a
published, unverifiable certification claim is a liability, not a shield. Instead it states the operator's
affirmation of compliance and Cyber Controller's disclaimer of independent certification. If the owner
supplies the actual certification / authorization documents, a specific citation can be added here (see the
``FCC_CERT_PENDING`` note); until then the language stands on authorized-use + user-responsibility grounds.
"""
from __future__ import annotations

APP_NAME = "Cyber Controller"

# Set to a real, owner-supplied certification / authorization reference to cite it specifically in the terms.
# Left empty on purpose: with no verifiable document, the terms must not assert a specific certificate. When
# the owner provides one, drop the reference string here and the "Regulatory" section will cite it.
FCC_CERT_PENDING: str = ""


def _regulatory_section() -> str:
    """The RF / regulatory clause — cites a specific certification only when one is actually on file
    (:data:`FCC_CERT_PENDING`); otherwise stands on operator-affirmation + user-responsibility."""
    if FCC_CERT_PENDING.strip():
        cert_line = (
            f"The operator is advised that this deployment is operated under the following authorization / "
            f"certification: {FCC_CERT_PENDING.strip()}. You remain responsible for operating within its scope."
        )
    else:
        cert_line = (
            "Cyber Controller makes no independent representation that it, or any device or firmware you "
            "connect to it, holds any particular FCC (or other jurisdiction) equipment certification. Where a "
            "function transmits radio energy or can cause interference, you operate it **only** under the "
            "licenses, authorizations, and equipment certifications that function requires, in a controlled "
            "environment, and you accept full responsibility for doing so lawfully."
        )
    return (
        "## 5. Radio, interference, and regulatory compliance\n\n"
        "Some firmware you may flash or drive with " + APP_NAME + " can transmit radio energy or is capable of "
        "causing interference (for example Wi‑Fi deauthentication, beacon/probe flooding, BLE advertising "
        "spam, or a jammer). In the United States, willful interference with radio communications is "
        "regulated (see 47 U.S.C. §333) and unlicensed devices are governed by FCC Part 15. Other "
        "jurisdictions have their own rules.\n\n"
        "- These interference-capable functions are intended for **controlled-laboratory or otherwise "
        "authorized testing** by qualified persons who hold the required licenses and authorizations.\n"
        "- " + APP_NAME + " is an orchestration and analysis surface: it sends commands to, and captures "
        "results from, firmware that **you** supply and run. It does not itself author or transmit radio "
        "frames.\n"
        "- " + cert_line + "\n"
    )


def terms_markdown() -> str:
    """The full Terms of Service + Terms of Use, as Markdown. Rendered in Help ▸ Terms."""
    return (
        "# " + APP_NAME + " — Terms of Service & Terms of Use\n\n"
        "_By installing, launching, or using " + APP_NAME + " you agree to these terms. If you do not "
        "agree, do not use the software._\n\n"

        "## 1. What this software is for\n\n"
        + APP_NAME + " is a tool for **security research and development, education, and authorized security "
        "testing**. It flashes and drives radio/security firmware and helps coordinate that hardware. It is "
        "provided for lawful, authorized use only.\n\n"

        "## 2. Who may use it\n\n"
        "You may use " + APP_NAME + " only if you are a **licensed or otherwise authorized professional** "
        "(or acting under one's direction) and you are permitted, under the law that applies to you, to "
        "perform the operations you choose to run. You are responsible for knowing and following that law.\n\n"

        "## 3. Authorized targets only\n\n"
        "You may direct " + APP_NAME + " and any connected device only at networks, devices, and radio "
        "spectrum that **you own or for which you have explicit, documented authorization** to test. "
        "Using it against systems you are not authorized to test may be a crime and is prohibited by these "
        "terms.\n\n"

        "## 4. Safety controls are yours to keep\n\n"
        + APP_NAME + " labels dangerous operations and asks you to confirm them; it does not silently perform "
        "them. You may reduce that friction in Settings, but doing so does not transfer responsibility — "
        "it remains entirely yours.\n\n"

        + _regulatory_section() + "\n"

        "## 6. Data\n\n"
        "Captures, logs, and wardrive data stay on your machine unless **you** choose to export or upload them "
        "(for example, uploading a wardrive CSV to a third-party service such as WiGLE, which is initiated only "
        "by you and governed by that service's own terms). You are responsible for what you collect and share, "
        "including any applicable privacy obligations.\n\n"

        "## 7. No warranty\n\n"
        "THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, "
        "INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, "
        "AND NON-INFRINGEMENT.\n\n"

        "## 8. Limitation of liability\n\n"
        "To the maximum extent permitted by law, the authors and contributors are not liable for any claim, "
        "damages, or other liability arising from the software or its use — including any use that is "
        "unlawful, unauthorized, or outside a controlled environment. You assume all risk.\n\n"

        "## 9. Acceptance\n\n"
        "If you use " + APP_NAME + ", you confirm that you have read, understood, and agree to these terms, "
        "and that your use is lawful and authorized.\n"
    )
