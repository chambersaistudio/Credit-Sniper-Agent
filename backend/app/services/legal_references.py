"""
Vetted legal and reporting-standard references. The reasoning engine and
dispute packages may cite ONLY these, by id — anything else a model
produces is dropped. Adding a reference is a reviewed code change.

Metro 2 entries name fields rather than field numbers: numbering must be
verified against the current CDIA Credit Reporting Resource Guide before
it is ever cited to a bureau or furnisher.

Not legal advice. These describe consumer rights at a high level so that
correspondence is specific; they are not a substitute for an attorney.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LegalReference:
    id: str
    citation: str
    title: str
    summary: str
    kind: str  # statute | regulation | reporting_standard
    applies_to: tuple[str, ...]  # bureau | furnisher


REFERENCES: dict[str, LegalReference] = {r.id: r for r in (
    LegalReference(
        "fcra_611_reinvestigation", "15 U.S.C. § 1681i(a)(1) (FCRA § 611)",
        "Reinvestigation of disputed information",
        "A consumer reporting agency must conduct a reasonable reinvestigation of disputed information, "
        "generally within 30 days of receiving the dispute (up to 15 more days if the consumer supplies "
        "additional relevant information during that period).",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_611_delete_unverified", "15 U.S.C. § 1681i(a)(5)(A) (FCRA § 611)",
        "Deletion or modification after reinvestigation",
        "If disputed information is found to be inaccurate or incomplete, or cannot be verified, the agency "
        "must promptly delete or modify it.",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_611_method_of_verification", "15 U.S.C. § 1681i(a)(6)(B)(iii), (a)(7) (FCRA § 611)",
        "Description of reinvestigation procedure",
        "After a reinvestigation the consumer may request a description of the procedure used to determine "
        "accuracy, including the business name and address of any furnisher contacted; the agency must "
        "provide it within 15 days of the request.",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_607b_accuracy", "15 U.S.C. § 1681e(b) (FCRA § 607(b))",
        "Maximum possible accuracy",
        "A consumer reporting agency must follow reasonable procedures to assure maximum possible accuracy "
        "of the information in a consumer report.",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_605_obsolete", "15 U.S.C. § 1681c(a), (c) (FCRA § 605)",
        "Obsolete information",
        "Accounts placed for collection or charged off, and most other adverse items, may not be reported "
        "more than 7 years after the period that begins 180 days after the delinquency began (exceptions in "
        "§ 1681c(b) for large credit or insurance transactions and high-salary employment).",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_609_disclosure", "15 U.S.C. § 1681g (FCRA § 609)",
        "Consumer file disclosure",
        "The consumer is entitled to disclosure of the information in their file and its sources. This is a "
        "disclosure right, not a dispute procedure — disputes are handled under § 611.",
        "statute", ("bureau",),
    ),
    LegalReference(
        "fcra_623a_furnisher_accuracy", "15 U.S.C. § 1681s-2(a)(1)-(3) (FCRA § 623(a))",
        "Furnisher duty of accuracy",
        "A furnisher may not report information it knows or has reasonable cause to believe is inaccurate, "
        "must correct information it determines is incomplete or inaccurate, and must report that "
        "information is disputed by the consumer.",
        "statute", ("furnisher",),
    ),
    LegalReference(
        "fcra_623a5_dofd", "15 U.S.C. § 1681s-2(a)(5) (FCRA § 623(a)(5))",
        "Furnisher must report the date of delinquency",
        "A furnisher reporting an account placed for collection or charged off must report the month and "
        "year the delinquency began, within 90 days of furnishing the information.",
        "statute", ("furnisher",),
    ),
    LegalReference(
        "fcra_623b_investigation", "15 U.S.C. § 1681s-2(b) (FCRA § 623(b))",
        "Furnisher investigation after notice from an agency",
        "After receiving notice of a dispute from a consumer reporting agency, the furnisher must "
        "investigate, review the information the agency provides, and report the results.",
        "statute", ("furnisher",),
    ),
    LegalReference(
        "reg_v_direct_dispute", "12 C.F.R. § 1022.43",
        "Direct disputes to furnishers (Regulation V)",
        "A furnisher must investigate a dispute the consumer sends directly to its designated address about "
        "the accuracy of information it furnished, and complete the investigation within the same period "
        "that applies to agencies under § 1681i(a)(1).",
        "regulation", ("furnisher",),
    ),
    LegalReference(
        "metro2_dofd", "CDIA Metro 2® Format — Date of First Delinquency",
        "Date of First Delinquency field",
        "The Metro 2 reporting standard requires furnishers to report the date of first delinquency for "
        "delinquent, charged-off, and collection accounts. Supporting evidence, not a legal violation on its own.",
        "reporting_standard", ("bureau", "furnisher"),
    ),
    LegalReference(
        "metro2_account_status", "CDIA Metro 2® Format — Account Status",
        "Account Status field",
        "The Metro 2 reporting standard defines account status codes that must reflect the account's current "
        "condition. Supporting evidence, not a legal violation on its own.",
        "reporting_standard", ("bureau", "furnisher"),
    ),
)}


def get_references(ids: list[str]) -> list[LegalReference]:
    return [REFERENCES[i] for i in ids if i in REFERENCES]


def catalog_for_prompt() -> str:
    return "\n".join(f"- {r.id}: {r.citation} — {r.title}. {r.summary}" for r in REFERENCES.values())
