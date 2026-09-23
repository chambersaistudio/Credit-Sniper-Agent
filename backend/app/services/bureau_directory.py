"""
Bureau dispute mailing addresses. These change occasionally; every package
tells the consumer to confirm the current address on the bureau's site
before mailing. Kept here (not in templates) so one edit updates everything.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BureauContact:
    name: str
    display_name: str
    dispute_address: str
    dispute_url: str


BUREAUS: dict[str, BureauContact] = {
    "equifax": BureauContact(
        "equifax", "Equifax Information Services LLC",
        "Equifax Information Services LLC\nP.O. Box 740256\nAtlanta, GA 30374-0256",
        "https://www.equifax.com/personal/credit-report-services/credit-dispute/",
    ),
    "experian": BureauContact(
        "experian", "Experian",
        "Experian\nP.O. Box 4500\nAllen, TX 75013",
        "https://www.experian.com/disputes/main.html",
    ),
    "transunion": BureauContact(
        "transunion", "TransUnion LLC",
        "TransUnion LLC\nConsumer Dispute Center\nP.O. Box 2000\nChester, PA 19016",
        "https://www.transunion.com/credit-disputes/dispute-your-credit",
    ),
}
