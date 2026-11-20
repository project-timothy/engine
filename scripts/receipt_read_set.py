"""Build the receipt page's eval set: 40 made-up receipt photos and what each
should read (Standalone Tim card R1, core/tools/receipt_read.py).

    uv run python scripts/receipt_read_set.py            # write the set
    uv run python scripts/receipt_read_set.py --check    # rebuild in memory, compare

Every receipt below is invented: made-up shops, made-up amounts. Each one is
drawn as a page (PDFium, already pinned, renders it), roughed up the way
phone photos of receipts are (faded thermal paper, a hand-written cash slip,
a crooked shot, a two-page bill), and written as a PNG, or as a two-page
image-only PDF. Nothing has a text layer: the model reads the picture, which
is what the page shows it. A case's ``text`` is what the receipt prints, for
the reader of a results file; ``expect`` is what the page must read off it.

**Built on a Mac.** PDFium draws the same page with different pixels on a
Mac and on Linux (CI's Linux, 2026-11-18: same size, different glyph and
speck pixels), so the pictures are this script's as built on macOS, and the
two real-model runs of 2026-11-18 scored these exact files. Two checks hold
the set:

- everywhere: every file matches ``manifest.json``, the SHA-256 of each file
  this script wrote, and the cases and ``job.json`` (plain JSON, the same
  on any machine) match a rebuild byte for byte;
- on a Mac (``--check``, and the test when it runs on macOS): each picture
  draws exactly what a rebuild draws, pixel row for pixel row
  (:func:`drawn`), however zlib packed it.

Rebuilding on another system draws new pictures: run the real-model eval
again before committing them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The drawing is shared with the letter and statement sets (card R2); the
# tests read _STREAM, MANIFEST, drawn, and picture off this module.
from picture_set import _STREAM, MANIFEST, ROOT, document, drawn, picture  # noqa: E402, F401
from picture_set import main as write_or_check  # noqa: E402

from core.agents.ap.extraction import _ATTACHED_ASK, _JSON_SPEC  # noqa: E402
from core.tools.receipt_read import CURRENCY, JOB  # noqa: E402

SET = ROOT / "core" / "llm" / "eval_sets" / JOB

# (name, look, the receipt's lines (a "\f" line breaks the page), expect)
# look: plain | faded | hand | tilted | two_pages
RECEIPTS: list[tuple[str, str, list[str], dict]] = [
    # ---- dollars ------------------------------------------------------------
    (
        "usd_grocery",
        "plain",
        [
            "CORNER FRESH MARKET",
            "118 Elm St, Dayton OH",
            "2026-10-03 14:22",
            "Bread 3.49",
            "Milk 4.19",
            "Apples 5.06",
            "SUBTOTAL 12.74",
            "TAX 0.89",
            "TOTAL $13.63",
            "VISA ****4410",
        ],
        {"amount": "13.63", "currency": "USD", "invoice_date": "2026-10-03"},
    ),
    (
        "usd_fuel_faded",
        "faded",
        [
            "PRAIRIE GAS #22",
            "Salina, KS",
            "Oct 14, 2026  07:41",
            "PUMP 4 UNLEADED",
            "11.208 GAL @ 3.199",
            "FUEL TOTAL $35.85",
            "THANK YOU",
        ],
        {"amount": "35.85", "currency": "USD", "invoice_date": "2026-10-14"},
    ),
    (
        "usd_diner_tip",
        "plain",
        [
            "BLUE PLATE DINER",
            "Asheville NC",
            "11/02/2026",
            "2 Specials 24.00",
            "Coffee x2 5.00",
            "Subtotal 29.00",
            "Tax 2.03",
            "Tip 5.00",
            "TOTAL $36.03",
        ],
        {"amount": "36.03", "currency": "USD", "invoice_date": "2026-11-02"},
    ),
    (
        "usd_office_tilted",
        "tilted",
        [
            "PAPER & INK SUPPLY",
            "Columbus OH",
            "Date: 2026-09-21",
            "Printer paper 2x 18.98",
            "Toner 64.50",
            "Subtotal 83.48",
            "Sales tax 6.05",
            "Total due $89.53",
        ],
        {"amount": "89.53", "currency": "USD", "invoice_date": "2026-09-21"},
    ),
    (
        "usd_hotel_two_pages",
        "two_pages",
        [
            "RIVERBEND INN",
            "Guest folio",
            "Arrive 2026-10-20",
            "Depart 2026-10-22",
            "10/20 Room 129.00",
            "10/20 Tax 16.77",
            "10/21 Room 129.00",
            "(continued)",
            "\f",
            "RIVERBEND INN  page 2",
            "10/21 Tax 16.77",
            "10/22 Parking 12.00",
            "BALANCE DUE $303.54",
            "Paid MC ****2201",
        ],
        {"amount": "303.54", "currency": "USD", "invoice_date": "2026-10-22"},
    ),
    (
        "usd_hand_cash",
        "hand",
        [
            "Received from Grace Church",
            "the sum of $45.00",
            "for 3 hours of van driving",
            "Nov 5, 2026",
            "signed  T. Moore",
        ],
        {"amount": "45.00", "currency": "USD", "invoice_date": "2026-11-05"},
    ),
    (
        "usd_pharmacy_faded",
        "faded",
        [
            "MAIN ST PHARMACY",
            "Lebanon TN",
            "2026-08-30",
            "Cough syrup 9.99",
            "Bandages 4.49",
            "Subtotal 14.48",
            "Tax 1.37",
            "TOTAL 15.85 USD",
        ],
        {"amount": "15.85", "currency": "USD", "invoice_date": "2026-08-30"},
    ),
    (
        "usd_airport_tilted",
        "tilted",
        [
            "SKYWAY NEWS & CAFE",
            "Concourse B, ATL",
            "Sep 3 2026 6:12 AM",
            "Bagel 3.75",
            "Water 3.29",
            "Tax 0.56",
            "TOTAL $7.60",
        ],
        {"amount": "7.60", "currency": "USD", "invoice_date": "2026-09-03"},
    ),
    # ---- Nepal --------------------------------------------------------------
    (
        "npr_market",
        "plain",
        [
            "BHATBHATENI MART",
            "Kathmandu, Nepal",
            "PAN 600112233",
            "Date: 2026-11-12",
            "Rice 5kg 1,150.00",
            "Lentils 2kg 640.00",
            "Oil 1L 410.00",
            "Sub Total 2,200.00",
            "VAT 13% 286.00",
            "Grand Total Rs. 2,486.00",
        ],
        {"amount": "2486.00", "currency": "NPR", "invoice_date": "2026-11-12"},
    ),
    (
        "npr_bus_hand",
        "hand",
        [
            "Pokhara Tourist Bus",
            "Kathmandu to Pokhara",
            "2 seats",
            "Rs 2,400/-",
            "12 Oct 2026",
            "paid cash",
        ],
        {"amount": "2400.00", "currency": "NPR", "invoice_date": "2026-10-12"},
    ),
    (
        "npr_hotel_faded",
        "faded",
        [
            "HOTEL LAKESIDE VIEW",
            "Pokhara-6, Nepal",
            "Bill date 2026-10-13",
            "Room 2 nights 6,000.00",
            "Breakfast 1,200.00",
            "Service charge 720.00",
            "VAT 1,029.60",
            "NET TOTAL NPR 8,949.60",
        ],
        {"amount": "8949.60", "currency": "NPR", "invoice_date": "2026-10-13"},
    ),
    (
        "npr_pharmacy_tilted",
        "tilted",
        [
            "Sita Pharmacy",
            "Lalitpur, Nepal",
            "2026-11-01",
            "Paracetamol 120.00",
            "ORS x5 125.00",
            "Total Rs. 245.00",
        ],
        {"amount": "245.00", "currency": "NPR", "invoice_date": "2026-11-01"},
    ),
    (
        "npr_printing_two_pages",
        "two_pages",
        [
            "NEW ERA PRINTERS",
            "Bhaktapur, Nepal",
            "Invoice date: 2026-09-15",
            "Tracts A5 x500 3,500.00",
            "Posters x40 2,800.00",
            "continued overleaf",
            "\f",
            "NEW ERA PRINTERS  p.2",
            "Binding 900.00",
            "Sub total 7,200.00",
            "VAT 13% 936.00",
            "TOTAL NPR 8,136.00",
        ],
        {"amount": "8136.00", "currency": "NPR", "invoice_date": "2026-09-15"},
    ),
    (
        "npr_tea_plain",
        "plain",
        [
            "HIMALAYAN TEA HOUSE",
            "Thamel, Kathmandu",
            "05 Nov 2026",
            "Masala tea x4 400.00",
            "Momo x2 560.00",
            "Total Rs 960.00",
        ],
        {"amount": "960.00", "currency": "NPR", "invoice_date": "2026-11-05"},
    ),
    # ---- Kenya, Uganda, Tanzania --------------------------------------------
    (
        "kes_supermarket",
        "plain",
        [
            "NAIVAS SUPERMARKET",
            "Nakuru, Kenya",
            "2026-10-08 17:05",
            "Maize flour 2kg 210.00",
            "Sugar 1kg 185.00",
            "Soap 3pk 330.00",
            "TOTAL KSh 725.00",
            "M-PESA",
        ],
        {"amount": "725.00", "currency": "KES", "invoice_date": "2026-10-08"},
    ),
    (
        "kes_matatu_hand",
        "hand",
        ["Matatu fare", "Nakuru - Nairobi", "4 people x 600", "KSh 2,400", "3 Nov 2026"],
        {"amount": "2400.00", "currency": "KES", "invoice_date": "2026-11-03"},
    ),
    (
        "kes_hardware_faded",
        "faded",
        [
            "MWANGI HARDWARE LTD",
            "Eldoret, Kenya",
            "Date 2026-09-29",
            "Cement 2 bags 1,640.00",
            "Nails 2kg 380.00",
            "VAT incl.",
            "TOTAL KES 2,020.00",
        ],
        {"amount": "2020.00", "currency": "KES", "invoice_date": "2026-09-29"},
    ),
    (
        "kes_clinic_tilted",
        "tilted",
        [
            "UZIMA MEDICAL CLINIC",
            "Kisumu, Kenya",
            "Receipt 0448  2026-10-30",
            "Consultation 1,000.00",
            "Lab test 1,500.00",
            "Amount paid KSh 2,500.00",
        ],
        {"amount": "2500.00", "currency": "KES", "invoice_date": "2026-10-30"},
    ),
    (
        "ugx_fuel",
        "plain",
        [
            "CITY OIL KAMPALA",
            "Kampala, Uganda",
            "2026-10-17",
            "Diesel 20.5 L",
            "@ 4,950",
            "TOTAL UGX 101,475",
        ],
        {"amount": "101475.00", "currency": "UGX", "invoice_date": "2026-10-17"},
    ),
    (
        "ugx_guesthouse_hand",
        "hand",
        ["Gulu Guest House", "1 night, 2 rooms", "UGX 160,000", "received 21 Sep 2026", "Okello"],
        {"amount": "160000.00", "currency": "UGX", "invoice_date": "2026-09-21"},
    ),
    (
        "tzs_bookshop",
        "faded",
        [
            "KILIMANJARO BOOKSHOP",
            "Arusha, Tanzania",
            "2026-10-02",
            "Bibles x6 90,000",
            "Notebooks x20 30,000",
            "TOTAL TSh 120,000",
        ],
        {"amount": "120000.00", "currency": "TZS", "invoice_date": "2026-10-02"},
    ),
    (
        "tzs_ferry_tilted",
        "tilted",
        [
            "ZANZIBAR FAST FERRY",
            "Dar es Salaam - Stone Town",
            "Travel date 2026-08-14",
            "2 adults economy",
            "Fare TZS 140,000",
        ],
        {"amount": "140000.00", "currency": "TZS", "invoice_date": None},
    ),
    # ---- Mozambique, South Africa, Zambia, Ethiopia, Ghana ------------------
    (
        "mzn_market",
        "plain",
        [
            "MERCADO CENTRAL",
            "Maputo, Mocambique",
            "Data 2026-11-10",
            "Filtros de agua x3 450,00",
            "Baldes x2 300,00",
            "TOTAL 750,00 MT",
        ],
        {"amount": "750.00", "currency": "MZN", "invoice_date": "2026-11-10"},
    ),
    (
        "mzn_chapa_hand",
        "hand",
        ["Recibo", "Transporte Beira - Dondo", "Valor: 1.200,00 MZN", "18 Out 2026"],
        {"amount": "1200.00", "currency": "MZN", "invoice_date": "2026-10-18"},
    ),
    (
        "mzn_hotel_two_pages",
        "two_pages",
        [
            "HOTEL POLANA SOL",
            "Maputo, Mocambique",
            "Factura  Data: 2026-09-09",
            "Quarto 3 noites 13.500,00",
            "Pequeno almoco 1.800,00",
            "(continua)",
            "\f",
            "HOTEL POLANA SOL  pag. 2",
            "Lavandaria 650,00",
            "IVA 16% 2.552,00",
            "TOTAL A PAGAR 18.502,00 MZN",
        ],
        {"amount": "18502.00", "currency": "MZN", "invoice_date": "2026-09-09"},
    ),
    (
        "mzn_fuel_faded",
        "faded",
        [
            "GALP NAMPULA",
            "Nampula, Mocambique",
            "2026-10-25 08:10",
            "Gasoleo 30,00 L",
            "Total MZN 2.598,00",
        ],
        {"amount": "2598.00", "currency": "MZN", "invoice_date": "2026-10-25"},
    ),
    (
        "zar_grocer",
        "plain",
        [
            "FRESHSTOP GROCER",
            "Durban, South Africa",
            "2026-10-11",
            "Groceries 412.60",
            "VAT incl 53.82",
            "TOTAL R 412.60",
        ],
        {"amount": "412.60", "currency": "ZAR", "invoice_date": "2026-10-11"},
    ),
    (
        "zar_rental_two_pages",
        "two_pages",
        [
            "CAPE CAR HIRE",
            "Cape Town, South Africa",
            "Invoice date 2026-08-28",
            "Rental 4 days 2,160.00",
            "Insurance 480.00",
            "see page 2",
            "\f",
            "CAPE CAR HIRE  page 2",
            "Fuel refill 615.00",
            "VAT 15% 488.25",
            "TOTAL DUE ZAR 3,743.25",
        ],
        {"amount": "3743.25", "currency": "ZAR", "invoice_date": "2026-08-28"},
    ),
    (
        "zmw_hardware_tilted",
        "tilted",
        [
            "LUSAKA BUILDERS MART",
            "Lusaka, Zambia",
            "2026-09-18",
            "Roofing sheets x4 1,320.00",
            "Screws 85.00",
            "TOTAL K 1,405.00",
        ],
        {"amount": "1405.00", "currency": "ZMW", "invoice_date": "2026-09-18"},
    ),
    (
        "etb_cafe_hand",
        "hand",
        ["Abebe Cafe", "Addis Ababa", "coffee and bread for 8", "Br 960", "Nov 9, 2026"],
        {"amount": "960.00", "currency": "ETB", "invoice_date": "2026-11-09"},
    ),
    (
        "ghs_print_faded",
        "faded",
        [
            "ACCRA COPY CENTRE",
            "Osu, Accra, Ghana",
            "2026-10-06",
            "Copies x300 150.00",
            "Binding x10 50.00",
            "TOTAL GHS 200.00",
        ],
        {"amount": "200.00", "currency": "GHS", "invoice_date": "2026-10-06"},
    ),
    # ---- India, Thailand, Philippines, Mexico, Europe ------------------------
    (
        "inr_train",
        "plain",
        [
            "INDIAN RAILWAYS",
            "New Delhi to Agra",
            "Journey 2026-10-19",
            "2 Adults, Chair Car",
            "Fare INR 1,830.00",
        ],
        {"amount": "1830.00", "currency": "INR", "invoice_date": None},
    ),
    (
        "inr_restaurant_faded",
        "faded",
        [
            "SPICE GARDEN RESTAURANT",
            "Bengaluru, India",
            "Date: 2026-09-26",
            "Thali x3 750.00",
            "Lassi x3 270.00",
            "CGST 2.5% 25.50",
            "SGST 2.5% 25.50",
            "Grand Total INR 1,071.00",
        ],
        {"amount": "1071.00", "currency": "INR", "invoice_date": "2026-09-26"},
    ),
    (
        "thb_guesthouse",
        "tilted",
        [
            "BAAN SUAN GUESTHOUSE",
            "Chiang Mai, Thailand",
            "2026-08-21",
            "2 nights 1,600.00",
            "Laundry 120.00",
            "Total THB 1,720.00",
        ],
        {"amount": "1720.00", "currency": "THB", "invoice_date": "2026-08-21"},
    ),
    (
        "php_jeepney_hand",
        "hand",
        [
            "Jeepney rental, Cebu",
            "whole day, youth camp",
            "PHP 3,500.00",
            "Oct 24, 2026",
            "Mang Rudy",
        ],
        {"amount": "3500.00", "currency": "PHP", "invoice_date": "2026-10-24"},
    ),
    (
        "mxn_hardware",
        "plain",
        [
            "FERRETERIA LA ESPERANZA",
            "Oaxaca, Mexico",
            "Fecha 2026-09-12",
            "Pintura 2 cubetas 1,180.00",
            "Brochas 96.00",
            "TOTAL MXN 1,276.00",
        ],
        {"amount": "1276.00", "currency": "MXN", "invoice_date": "2026-09-12"},
    ),
    (
        "eur_train_faded",
        "faded",
        [
            "DEUTSCHE BAHN",
            "Frankfurt - Koln",
            "Reisedatum 2026-09-04",
            "1 Erwachsener 2. Klasse",
            "Summe 49,90 EUR",
        ],
        {"amount": "49.90", "currency": "EUR", "invoice_date": None},
    ),
    (
        "eur_cafe_symbol",
        "plain",
        [
            "CAFE DO PORTO",
            "Lisboa, Portugal",
            "2026-11-04",
            "2 Galao 3,60",
            "2 Pastel de nata 2,80",
            "TOTAL €6,40",
        ],
        {"amount": "6.40", "currency": "EUR", "invoice_date": "2026-11-04"},
    ),
    (
        "gbp_taxi_hand",
        "hand",
        ["Heathrow Cars", "LHR to Oxford", "£85.00 paid", "1 Oct 2026", "driver J. Patel"],
        {"amount": "85.00", "currency": "GBP", "invoice_date": "2026-10-01"},
    ),
    (
        "gbp_hotel_two_pages",
        "two_pages",
        [
            "THE KINGS ARMS",
            "York, United Kingdom",
            "Invoice date 2026-09-30",
            "Room 2 nights 210.00",
            "Dinner 46.50",
            "continued",
            "\f",
            "THE KINGS ARMS  page 2",
            "Breakfast x2 25.00",
            "VAT included",
            "TOTAL GBP 281.50",
        ],
        {"amount": "281.50", "currency": "GBP", "invoice_date": "2026-09-30"},
    ),
]


TICKETS = frozenset({"inr_train", "eur_train_faded", "tzs_ferry_tilted"})
"""Tickets that print only a day of travel: a travel date is not the day it
was paid, so the reader leaves the day blank and Tim asks her."""


def _kind(lines: list[str]) -> dict:
    """``doc_type`` where it is the receipt's own plain answer. A bill that
    prints "Invoice" (or "Factura") is a receipt to one careful reader and an
    invoice to another, and the page reads the two alike, so it is not asked
    (the first real run, 2026-11-18: four such bills read right, as invoices)."""
    printed = " ".join(lines).casefold()
    return {} if "invoice" in printed or "factura" in printed else {"doc_type": "receipt"}


def build() -> dict[str, bytes]:
    """Every file of the set, by its path under the set's directory."""
    files: dict[str, bytes] = {}
    job = {
        "job": JOB,
        "output_model": "core.tools.receipt_read:PageReading",
        "decimal_fields": ["amount"],
        "prompt_source": "core/agents/ap/extraction.py (the expense lane's one extractor) plus "
        "core/tools/receipt_read.py CURRENCY, as the receipt page sends them; documents and "
        "cases built by scripts/receipt_read_set.py",
        "built_by": "scripts/receipt_read_set.py",
        "system": _JSON_SPEC + CURRENCY,
        "user": _ATTACHED_ASK,
    }
    files["job.json"] = (json.dumps(job, indent=2) + "\n").encode()
    for name, look, lines, expect in RECEIPTS:
        file, data = document(name, look, lines)
        files[f"documents/{file}"] = data
        kind = look.replace("_", " ")
        note = f"a {kind} receipt: the total, its currency, and its day read off the picture"
        if name in TICKETS:
            note = (
                f"a {look.replace('_', ' ')} ticket that prints only the day of travel: the total "
                "and its currency read off it, and no day it was paid, so Tim asks her (the "
                "owner's call of 2026-11-19)"
            )
        case = {
            "name": name,
            "note": note,
            "document": file,
            "text": "\n".join(lines),
            "expect": {**_kind(lines), **expect},
        }
        files[f"cases/{name}.json"] = (
            json.dumps(case, indent=2, ensure_ascii=False) + "\n"
        ).encode()
    return files


def main(argv: list[str]) -> int:
    return write_or_check(argv, SET, build(), f"{len(RECEIPTS)} receipts")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
