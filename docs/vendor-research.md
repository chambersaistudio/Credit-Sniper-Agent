# Vendor and tool research for later phases

Researched September 2026. Nothing here is integrated yet — Phase 1.5 built the
foundation these plug into: recipient-independent dispute packages and a
case state machine that records how and when each was sent. Claims marked *(vendor)* come
from the vendor's own site or a competitor's comparison page and must be
confirmed with the vendor before building on them.

Access categories, as requested:

1. **Public API** — self-serve signup, documented, usable without a contract.
2. **Enterprise / approval-only API** — requires a contract, credentialing, or a permissible-purpose review.
3. **CRA / furnisher-only infrastructure** — not available to consumers or consumer apps at all.
4. **Consumer portal, no supported API** — a website a consumer uses by hand.
5. **Browser-automation fallback** — driving (4) with a browser on the consumer's behalf.

## Structured consumer credit data (Phase 3: live monitoring)

| Provider | Category | What it offers | Fit for Credit Sniper |
|---|---|---|---|
| **Array** | 2 | Embeddable, white-label consumer credit monitoring: tri-bureau reports and scores, pushed to the consumer inside your app *(vendor)*. | **Best fit.** Built for consumer-facing apps where the consumer is the one viewing their own data. Needs a partnership agreement. |
| **Bloom Credit** | 2 | Tri-bureau data access plus Metro 2 furnishing, REST/GraphQL, sandbox with fake consumers *(vendor)*. | Plausible for data access; the sandbox is useful for development. Furnishing is irrelevant to us. |
| **iSoftpull**, **Soft Pull Solutions**, **CRS Credit API**, MicroBilt | 2 | Soft/hard pulls with JSON output, aimed at lenders' prequalification and underwriting *(vendor)*. | Weak fit. These credential businesses with a lending permissible purpose. A consumer-directed app relies on the consumer's written instruction (15 U.S.C. § 1681b(a)(2)); confirm each vendor supports that use case, and whether bureaus restrict dispute/credit-repair-adjacent products. |
| **AnnualCreditReport.com** and the bureau consumer sites | 4 | Free reports from each bureau. | What Phase 1.5 uses today, via PDF upload. |
| Bureau direct APIs | 2 | Each bureau sells data access to credentialed businesses. | Only worth pursuing at scale; an aggregator above is the practical path. |

## Dispute submission

| Channel | Category | Notes |
|---|---|---|
| **e-OSCAR** (ACDV/AUD) | 3 | The system bureaus and furnishers use to route and answer disputes. Owned by the bureaus; access is only for CRAs and data furnishers. **Not available to Credit Sniper** — our disputes enter e-OSCAR only when a bureau forwards them. |
| Bureau online dispute centers (Equifax, Experian, TransUnion) | 4 → 5 | No supported consumer API. Automating them is the Phase 2 browser layer. Before building: review each bureau's terms of use on automated access, keep the consumer's own login, and have the consumer complete any CAPTCHA or MFA (see browser automation below). |
| Mail to bureau dispute addresses | — | Always available; addresses live in `backend/app/services/bureau_directory.py`. |
| Furnisher direct disputes (12 C.F.R. § 1022.43) | 4 / mail | Must go to the furnisher's designated dispute address. Some furnishers have portals; most are mail. |

## Print-and-mail and Certified Mail (Phase 2/3 execution channel)

| Provider | Category | Certified Mail | Return receipt / tracking |
|---|---|---|---|
| **LetterStream** | 1 | Yes, via API | API returns USPS tracking scans and **electronic return-receipt signatures** *(vendor)* — the delivery-date evidence the case deadline wants. |
| **Lob** | 1 | Yes — `extra_services: "certified"` on letter create *(vendor docs)* | Tracking events via webhooks; confirm electronic return receipt support. |
| **PostGrid** | 1 | Yes, via Letter API *(vendor)* | Confirm return-receipt and tracking-event details. |
| Simple Certified Mail | 1 | API *(vendor)* | Confirm. |

**Recommendation:** LetterStream or Lob, chosen on whether the electronic
return receipt (delivery date + signature) is available through the API,
because `POST /api/cases/{id}/delivered` exists to receive exactly that date.

## Browser automation (Phase 2)

| Tool | Category | Notes |
|---|---|---|
| **Playwright** | 1 (open source) | Already installed in this environment; deterministic scripts per portal. |
| **Stagehand** | 1 (open source SDK on Playwright) | Natural-language `act` / `extract` / `observe` on top of Playwright — more resilient to portal layout changes than hard-coded selectors. |
| **Browserbase** | 1 (paid hosted browsers) | Session replay, human-in-the-loop templates that pause for user input. It also offers automatic CAPTCHA solving and residential proxies *(vendor)* — **don't use those against bureau portals**: they exist to defeat anti-automation controls, which conflicts with portal terms and the project's own rule of acting only with the consumer's consent. Hand CAPTCHAs and MFA to the consumer on their phone instead. |

## Document ingestion

| Tool | Category | Notes |
|---|---|---|
| pdfplumber / pypdf | 1 (open source) | Current parser for text PDFs. |
| Claude PDF document input | 1 | For scanned/image-only PDFs, which the upload currently rejects. Would slot into the existing AI extraction fallback, keeping its verify-against-source step (checking against OCR text instead). |
| AWS Textract / Google Document AI | 1 | Alternative OCR; adds a vendor and another place report data is sent. |

## Email monitoring (bureau response notifications)

| Tool | Category | Notes |
|---|---|---|
| **Gmail API** | 1, with review | Reading mail needs restricted OAuth scopes; production use requires Google's app verification and security assessment. Plan for that lead time. |
| Microsoft Graph (Outlook) | 1, with review | Admin/user consent flows. |
| IMAP + app password | 1 | Simplest; the user must create an app password; weaker security posture. |

## 2026 regulatory context that affects strategy

- **CFPB's proposed FCRA data-broker rule** (Dec 2024), which would also have
  changed dispute handling, was **withdrawn in May 2025**. No new federal
  dispute procedures to build for.
- **CFPB's medical-debt rule was vacated** by the Eastern District of Texas on
  **July 11, 2025**. No federal rule keeps medical debt off reports; the
  bureaus' voluntary 2023 policy (no paid medical collections, none under
  $500) remains, and some states restrict it further. Worth adding as a
  deterministic rule: a medical collection under $500 or marked paid that is
  still reporting is a finding.

## Sources

- [CRS Credit API — best credit reporting APIs 2026 (vendor comparison)](https://crscreditapi.com/best-credit-reporting-apis/)
- [iSoftpull integration suite](https://www.isoftpull.com/technology/integration-suite)
- [Soft Pull Solutions — API integration](https://www.softpullsolutions.com/api-integration/)
- [Array — consumer credit solutions](https://array.com/vertical-solutions/credit-services)
- [Bloom Credit developer docs — ordering credit data](https://developers.bloomcredit.io/docs/ordering-credit-data)
- [e-OSCAR — getting started](https://www.e-oscar.org/gettingstarted)
- [LetterStream — certified mail letters](https://www.letterstream.com/certifiedmailletters/)
- [Lob Help Center — certified or registered mail](https://help.lob.com/print-and-mail/building-a-mail-strategy/mailing-classes-and-postage/certified-mail-or-registered-mail)
- [PostGrid — Letter API](https://www.postgrid.com/letter-api/)
- [Simple Certified Mail — API](https://www.simplecertifiedmail.com/api/)
- [Stagehand on GitHub](https://github.com/browserbase/stagehand)
- [Browserbase — human-in-the-loop template](https://www.browserbase.com/templates/agent-with-human-in-loop)
- [Consumer Financial Services Law Monitor — CFPB withdraws proposed FCRA data broker rule](https://www.consumerfinancialserviceslawmonitor.com/2025/05/cfpb-withdraws-proposed-fcra-data-broker-rule/)
- [CFPB — Regulation V medical information rule (vacated)](https://www.consumerfinance.gov/rules-policy/final-rules/prohibition-on-creditors-and-consumer-reporting-agencies-concerning-medical-information-regulation-v/)
- [Brownstein — court vacates CFPB medical debt rule](https://www.bhfs.com/insight/federal-court-vacates-cfpbs-medical-debt-rule-finds-fcra-preempts-state-laws/)
