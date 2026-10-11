"""Generate sample invoice PDFs for testing.

    venv\\python.exe scripts\\make_sample_invoices.py

Four invoices from four fictional vendors, each laid out differently on
purpose. If every sample used the same template, extraction that merely
memorised that template would look like extraction that works. These
differ in the ways real invoices differ:

  - what the number is called:  "Invoice #", "Invoice No.", "Ref", "Doc"
  - date format:                ISO, DD/MM/YYYY, "28 Sep 2026", US
  - what the total is called:   "TOTAL", "Amount Due", "Grand Total", "Net"
  - currency:                   USD, INR, EUR, GBP -- symbol or code
  - structure:                  table, right-aligned block, plain lines
  - noise:                      subtotals and tax lines that are NOT the
                                total, which is the easiest thing to get
                                wrong

Written to sandbox/invoices/ so the agent's file tools can reach them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from core.config import SANDBOX_DIR  # noqa: E402

OUT_DIR = SANDBOX_DIR / "invoices"

# The truth, for checking extraction against. Not shipped to the agent --
# this is what WE compare its answers to.
EXPECTED = {
    "acme.pdf": ("ACME CORPORATION", "INV-2026-0417", "2026-09-28", 2540.50, "USD"),
    "globex.pdf": ("Globex Ltd", "GLX/2026/118", "2026-10-02", 98400.00, "INR"),
    "initech.pdf": ("Initech GmbH", "IT-9930", "2026-09-15", 1875.60, "EUR"),
    "umbrella.pdf": ("Umbrella Supplies", "DOC-55120", "2026-10-07", 640.00, "GBP"),
}


def _text(c: canvas.Canvas, x: float, y: float, s: str, size: int = 10, bold=False):
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, s)


def acme(path: Path) -> None:
    """Classic layout: header block, line-item table, total at the bottom."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 20 * mm, 275 * mm, "ACME CORPORATION", 16, True)
    _text(c, 20 * mm, 269 * mm, "1200 Industrial Way, Springfield")
    _text(c, 20 * mm, 258 * mm, "Invoice #: INV-2026-0417", 11, True)
    _text(c, 20 * mm, 252 * mm, "Date: 2026-09-28")
    _text(c, 20 * mm, 246 * mm, "Bill To: Northwind Trading")

    _text(c, 20 * mm, 230 * mm, "Description", 10, True)
    _text(c, 110 * mm, 230 * mm, "Qty", 10, True)
    _text(c, 130 * mm, 230 * mm, "Rate", 10, True)
    _text(c, 160 * mm, 230 * mm, "Amount", 10, True)
    c.line(20 * mm, 228 * mm, 190 * mm, 228 * mm)

    rows = [
        ("Consulting services", "12", "150.00", "1800.00"),
        ("Cloud hosting (September)", "1", "240.50", "240.50"),
        ("Support retainer", "1", "500.00", "500.00"),
    ]
    y = 221 * mm
    for desc, qty, rate, amount in rows:
        _text(c, 20 * mm, y, desc)
        _text(c, 110 * mm, y, qty)
        _text(c, 130 * mm, y, rate)
        _text(c, 160 * mm, y, amount)
        y -= 7 * mm

    c.line(130 * mm, y - 1 * mm, 190 * mm, y - 1 * mm)
    _text(c, 130 * mm, y - 8 * mm, "TOTAL:", 11, True)
    _text(c, 160 * mm, y - 8 * mm, "USD 2540.50", 11, True)
    _text(c, 20 * mm, 40 * mm, "Payment due within 30 days.")
    c.save()


def globex(path: Path) -> None:
    """Right-aligned detail block, DD/MM/YYYY dates, subtotal + GST noise."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 20 * mm, 275 * mm, "Globex Ltd", 18, True)
    _text(c, 20 * mm, 268 * mm, "Plot 44, Whitefield, Bengaluru 560066")
    _text(c, 20 * mm, 262 * mm, "GSTIN: 29AABCG1234F1Z5")

    _text(c, 120 * mm, 250 * mm, "Invoice No.", 10, True)
    _text(c, 160 * mm, 250 * mm, "GLX/2026/118")
    _text(c, 120 * mm, 244 * mm, "Invoice Date", 10, True)
    _text(c, 160 * mm, 244 * mm, "02/10/2026")
    _text(c, 120 * mm, 238 * mm, "Due Date", 10, True)
    _text(c, 160 * mm, 238 * mm, "01/11/2026")

    _text(c, 20 * mm, 220 * mm, "Annual maintenance contract -- 12 months")
    _text(c, 160 * mm, 220 * mm, "83389.83")
    _text(c, 20 * mm, 213 * mm, "On-site engineer visits (4)")
    _text(c, 160 * mm, 213 * mm, "0.00")

    _text(c, 120 * mm, 198 * mm, "Subtotal")
    _text(c, 160 * mm, 198 * mm, "83389.83")
    _text(c, 120 * mm, 192 * mm, "GST @ 18%")
    _text(c, 160 * mm, 192 * mm, "15010.17")
    _text(c, 120 * mm, 184 * mm, "Amount Due (INR)", 11, True)
    _text(c, 160 * mm, 184 * mm, "98,400.00", 11, True)
    c.save()


def initech(path: Path) -> None:
    """Minimal: no table at all, euro symbol, long-form date."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 25 * mm, 270 * mm, "Initech GmbH", 14, True)
    _text(c, 25 * mm, 264 * mm, "Rechnung / Invoice")
    _text(c, 25 * mm, 250 * mm, "Ref: IT-9930")
    _text(c, 25 * mm, 244 * mm, "Issued: 15 September 2026")
    _text(c, 25 * mm, 238 * mm, "Customer: Northwind Trading GmbH")
    _text(c, 25 * mm, 222 * mm, "Software licence renewal, 25 seats")
    _text(c, 25 * mm, 216 * mm, "Billing period: Oct 2026 - Sep 2027")
    _text(c, 25 * mm, 200 * mm, "Grand Total  EUR 1.875,60", 12, True)
    _text(c, 25 * mm, 190 * mm, "(VAT reverse charge applies)")
    c.save()


def umbrella(path: Path) -> None:
    """Awkward on purpose: 'Doc' not 'Invoice', currency symbol, US date,
    and a larger 'Credit limit' figure to distract from the real total."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 20 * mm, 272 * mm, "UMBRELLA SUPPLIES", 15, True)
    _text(c, 20 * mm, 266 * mm, "Unit 7, Dockside Estate, Manchester")
    _text(c, 20 * mm, 254 * mm, "Doc: DOC-55120     Raised: 10/07/2026 (US format)")
    _text(c, 20 * mm, 248 * mm, "Account: NW-0042     Credit limit: GBP 25,000.00")

    _text(c, 20 * mm, 232 * mm, "Item", 10, True)
    _text(c, 150 * mm, 232 * mm, "Line total", 10, True)
    _text(c, 20 * mm, 225 * mm, "Protective gloves, box of 100 x 8")
    _text(c, 150 * mm, 225 * mm, "320.00")
    _text(c, 20 * mm, 218 * mm, "Safety goggles x 40")
    _text(c, 150 * mm, 218 * mm, "320.00")
    _text(c, 20 * mm, 204 * mm, "Net payable", 11, True)
    _text(c, 150 * mm, 204 * mm, "GBP 640.00", 11, True)
    c.save()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    builders = {
        "acme.pdf": acme,
        "globex.pdf": globex,
        "initech.pdf": initech,
        "umbrella.pdf": umbrella,
    }
    for name, build in builders.items():
        build(OUT_DIR / name)
        print(f"  wrote {OUT_DIR / name}")
    print(f"\n{len(builders)} invoices in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
