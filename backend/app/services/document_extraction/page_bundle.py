"""
Transient page bundles for batched extraction.

Stage 2 reads four tradelines at a time. Re-sending the whole 31-page
disclosure for each batch would pay for the entire document four times over,
so each batch gets a bundle holding only the pages its tradelines live on.

Two rules make this safe:

* **The Stage-1 index chooses the pages.** Never a regex, never a text search,
  never a guess from the page's contents. The index was produced by a model
  reading the full original document, and it is the only authority on where a
  tradeline is.
* **The bundle is transient; the original is authoritative.** The bundle is
  built in memory, handed to one model call, and dropped. What is stored in R2
  never changes, and every page number that survives into extracted data is an
  ORIGINAL page number, remapped here rather than reported by the model.

The model sees a short document and numbers its pages 1..N. `PageBundle` holds
the deterministic map back to the original numbering, so provenance recorded
against a bundle page always resolves to the page of the real report.
"""
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Iterable


class PageSelectionError(ValueError):
    """The requested pages cannot be turned into a bundle."""


@dataclass(frozen=True)
class PageBundle:
    """A PDF holding a subset of another PDF's pages, plus the way back.

    `pages[i]` is the ORIGINAL 1-based page number of the bundle's (i+1)-th
    page, so the mapping is total, ordered and reversible."""

    pdf: bytes
    pages: tuple[int, ...]
    source_page_count: int
    # Pages added only for safety margin, not because the index named them.
    padding: tuple[int, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_original(self, bundle_page: int | None) -> int | None:
        """Bundle page number -> original page number.

        Returns None for anything outside the bundle rather than guessing: a
        page reference we cannot place is worse than no page reference, since
        it would be indistinguishable from real provenance later."""
        if bundle_page is None or bundle_page < 1 or bundle_page > len(self.pages):
            return None
        return self.pages[bundle_page - 1]

    def describe(self) -> str:
        """How the bundle is explained to the model, in ITS numbering."""
        return ", ".join(
            f"page {i}" for i in range(1, len(self.pages) + 1)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": list(self.pages),
            "padding": list(self.padding),
            "page_count": self.page_count,
            "source_page_count": self.source_page_count,
        }


def select_pages(
    indexed_pages: Iterable[Iterable[int]],
    *,
    total_pages: int,
    context_pages: int = 0,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Which original pages a batch needs, and which of those are padding.

    Every page the index named for any tradeline in the batch is included —
    a tradeline spanning pages 9 and 10 gets both. `context_pages` adds that
    many neighbouring pages on each side as a safety margin, for a tradeline
    that spills past where the index said it ended.

    Deterministic: the same index entries and the same margin always produce
    the same page set, in ascending order.
    """
    named: set[int] = set()
    for pages in indexed_pages:
        for page in pages or ():
            if 1 <= page <= total_pages:
                named.add(int(page))
    if not named:
        raise PageSelectionError("no indexed pages for this batch")

    padded: set[int] = set(named)
    for page in named:
        for offset in range(1, context_pages + 1):
            for neighbour in (page - offset, page + offset):
                if 1 <= neighbour <= total_pages:
                    padded.add(neighbour)

    return tuple(sorted(padded)), tuple(sorted(padded - named))


def build_bundle(document: bytes, pages: Iterable[int], *, padding: Iterable[int] = ()) -> PageBundle:
    """Extract `pages` (original, 1-based) from `document` into a new PDF.

    The source document is never modified — pypdf reads it and writes a fresh
    file — so the authoritative original is untouched by construction.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(document))
    total = len(reader.pages)
    wanted = sorted({int(p) for p in pages})
    if not wanted:
        raise PageSelectionError("a bundle needs at least one page")
    out_of_range = [p for p in wanted if p < 1 or p > total]
    if out_of_range:
        raise PageSelectionError(
            f"pages {out_of_range} are outside this {total}-page document"
        )

    writer = PdfWriter()
    for page in wanted:
        writer.add_page(reader.pages[page - 1])  # pypdf is 0-based
    buffer = BytesIO()
    writer.write(buffer)
    return PageBundle(
        pdf=buffer.getvalue(),
        pages=tuple(wanted),
        source_page_count=total,
        padding=tuple(sorted(set(padding) & set(wanted))),
    )


def page_count(document: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(BytesIO(document)).pages)


@dataclass
class RemapReport:
    """What happened when bundle page numbers were translated back."""

    remapped: int = 0
    unmapped: int = 0
    # Bundle page numbers the model reported that the bundle does not have.
    out_of_range: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"remapped": self.remapped, "unmapped": self.unmapped,
                "out_of_range": sorted(set(self.out_of_range))}


def remap_tradelines(tradelines: list, bundle: PageBundle) -> RemapReport:
    """Rewrite every page reference on these tradelines to original pages.

    Mutates in place and reports what could not be placed. Covers all three
    places a page number appears: the tradeline's own `source_pages`, each
    `field_evidence` entry, and each month of `payment_history`.
    """
    report = RemapReport()

    def translate(bundle_page: int | None) -> int | None:
        original = bundle.to_original(bundle_page)
        if original is None:
            if bundle_page is not None:
                report.out_of_range.append(bundle_page)
            report.unmapped += 1
        else:
            report.remapped += 1
        return original

    for tradeline in tradelines:
        mapped = [p for p in (translate(page) for page in (tradeline.source_pages or [])) if p]
        # Deduplicated and ordered so provenance reads the same every run.
        tradeline.source_pages = sorted(set(mapped))
        for evidence in tradeline.field_evidence or []:
            evidence.page = translate(evidence.page)
        for month in tradeline.payment_history or []:
            month.source_page = translate(month.source_page)
    return report
