"""Reading documents: PDF text extraction.

One tool. It returns the text of a PDF and nothing more -- it does not try
to understand the document, find a total or identify a vendor.

That restraint is deliberate, and it is the most interesting design
decision in this file. The obvious thing to build is
`extract_invoice_fields(path) -> vendor, number, total`, which would need
a model call, and tools must not depend on `llm/` -- they have to stay
testable with no API key.

But the agent already has the machinery. `resolve_arguments` in the
executor turns an earlier step's output into a later step's arguments,
guided by that step's intent. So the plan becomes:

    1. read_pdf("invoices/acme.pdf")          -> raw text
    2. add_invoice(vendor={{step_1.output}},  -> the resolver pulls each
                   total_amount={{step_1...}})   field out of that text

No invoice-specific extraction code exists anywhere, and the same two
steps work for a receipt, a purchase order or a contract. A dedicated
extractor would have been an invoice-shaped workflow -- exactly the
hardcoding this project is meant to avoid.
"""

from __future__ import annotations

import pdfplumber

from core.schemas import Risk
from tools.files import FileAccessError, _resolve
from tools.registry import tool

# PDFs can be enormous. A page cap keeps a 400-page scan from silently
# consuming the model's whole context window.
MAX_PAGES = 20


@tool(risk=Risk.READ)
def read_pdf(path: str) -> str:
    """Extract the text of a PDF in the sandbox, page by page.

    Use this for invoices, receipts and other PDF documents. Pages are
    separated by '--- page N ---' markers. Returns the text only; layout,
    images and formatting are lost, so figures may appear in a different
    order than they look on the page.
    """
    resolved = _resolve(path, must_exist=True)

    if resolved.suffix.lower() != ".pdf":
        raise FileAccessError(
            f"{path!r} is not a PDF. Use read_file for text files."
        )

    try:
        with pdfplumber.open(resolved) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise FileAccessError(
                    f"{path!r} has {len(pdf.pages)} pages, over the "
                    f"{MAX_PAGES} page limit."
                )
            pages = [
                f"--- page {number} ---\n{page.extract_text() or ''}"
                for number, page in enumerate(pdf.pages, start=1)
            ]
    except FileAccessError:
        raise
    except Exception as exc:
        raise FileAccessError(
            f"Could not read {path!r} as a PDF: {type(exc).__name__}: {exc}"
        ) from exc

    text = "\n\n".join(pages).strip()

    if not text.replace("--- page 1 ---", "").strip():
        # A scanned PDF is images, not text. Saying so is far more useful
        # than returning an empty string the agent would treat as an empty
        # document and quietly build wrong conclusions on.
        raise FileAccessError(
            f"{path!r} contains no extractable text -- it is probably a "
            "scanned image. OCR would be needed to read it."
        )

    return text
