"""House-style response template.

Implements the block structure documented in the companion "FOI Response
Template & House-Style Guide", derived from Hertfordshire County Council's
published FOI guidance. The drafting service assembles these blocks; blocks
that do not apply are omitted rather than left empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import get_settings
from ..enums import HoldingStatus, Regime

settings = get_settings()

REGIME_NAME = {
    Regime.FOIA.value: "Freedom of Information Act 2000",
    Regime.EIR.value: "Environmental Information Regulations 2004",
}

HOLDING_SENTENCE = {
    HoldingStatus.HELD.value:
        "I can confirm that {council} does hold the information you have requested. "
        "Please see our response to each of your questions below.",
    HoldingStatus.PARTIAL.value:
        "I can confirm that {council} holds some of the information you have requested. "
        "Please see our response to each of your questions below.",
    HoldingStatus.NOT_HELD.value:
        "Having made appropriate enquiries, I can confirm that {council} does not hold "
        "the information you have requested.",
    HoldingStatus.UNKNOWN.value:
        "We are still determining whether {council} holds the information you have requested.",
}


@dataclass
class QA:
    question: str
    answer: str
    citation: str | None = None


@dataclass
class ResponseContext:
    reference: str
    requester_name: str
    received: date
    regime: str
    holding_status: str
    qas: list[QA] = field(default_factory=list)
    exemptions: list[dict] = field(default_factory=list)   # {section,is_qualified,pit,reasoning}
    fees_note: str | None = None


def _salutation(name: str) -> str:
    name = (name or "").strip()
    return f"Dear {name}," if name else "Dear Sir/Madam,"


def render(ctx: ResponseContext) -> str:
    """Return the full response letter as plain text in the council's house style."""
    s = settings
    act = REGIME_NAME.get(ctx.regime, REGIME_NAME[Regime.FOIA.value])
    lines: list[str] = []

    # 1 Header & reference
    lines += [s.council_address, ""]
    lines += [f"Our ref: {ctx.reference}", f"Date: {ctx.received.strftime('%d %B %Y')}", ""]

    # 2 Salutation + subject
    lines += [_salutation(ctx.requester_name), ""]
    lines += [f"Request for information under the {act}", ""]

    # 3 Acknowledgement
    lines += [
        f"Thank you for your request for information received on "
        f"{ctx.received.strftime('%d %B %Y')}, which has been considered under the {act}.",
        "",
    ]

    # 4 Confirm or deny
    lines += [HOLDING_SENTENCE[ctx.holding_status].format(council=s.council_name), ""]

    # 5 The response (question by question)
    if ctx.holding_status in (HoldingStatus.HELD.value, HoldingStatus.PARTIAL.value) and ctx.qas:
        lines += ["You asked:", ""]
        for i, qa in enumerate(ctx.qas, start=1):
            lines += [f"{i}. {qa.question}"]
            answer = qa.answer
            if qa.citation:
                answer = f"{answer} [Source: {qa.citation}]"
            lines += [f"   Our response: {answer}", ""]

    # 6 Exemptions
    for ex in ctx.exemptions:
        sec = ex.get("section", "")
        lines += [f"Information withheld under section {sec} of the {act}"]
        if ex.get("is_qualified", True):
            lines += [
                "This is a qualified exemption, so we have carried out a public-interest test. "
                + ex.get("reasoning", "")
                + f" On balance we have concluded that the public interest favours "
                f"{'disclosing' if ex.get('pit') == 'disclose' else 'withholding'} this information."
            ]
        else:
            lines += ["This is an absolute exemption. " + ex.get("reasoning", "")]
        lines += [""]

    # 7 Fees notice
    if ctx.fees_note:
        lines += [ctx.fees_note, ""]

    # 8 Re-use of information
    lines += [
        "Re-use of information",
        f"Information released under the {act} may only be re-used with the council's "
        "permission. Re-use without permission may breach copyright.",
        "",
    ]

    # 9 Right to review / appeal
    lines += [
        "If you are dissatisfied",
        f"If you are dissatisfied with the handling of your request, you may ask for an "
        f"internal review by writing to the {s.responding_team} at the address above. "
        f"If you remain dissatisfied after the internal review, you may complain to the "
        f"{s.ico_address} (www.ico.org.uk).",
        "",
    ]

    # 10 Sign-off
    lines += ["Yours sincerely,", s.responding_team, s.council_name]

    return "\n".join(lines)
