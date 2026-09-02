"""Turning uploads into markdown the ingester understands (§9, §15.1).

No PDF fixture is generated here: the PDF path is a thin call into pypdf and
the failure that matters -- a scan with no text -- is asserted through the
same error the panel shows. The formats the operator is most likely to bring
from a spreadsheet or a website are exercised in full.
"""

from __future__ import annotations

import pytest

from uaagro_domain.errors import ValidationError
from voice_worker.knowledge.extract import (
    Extracted,
    _Page,
    _same_site,
    doc_type_for,
    extract_file,
    extract_many,
)


def test_the_format_comes_from_the_filename_not_the_content_type() -> None:
    assert doc_type_for("catalogue.PDF") == "pdf"
    assert doc_type_for("prices.csv") == "csv"
    assert doc_type_for("notes.md") == "markdown"
    with pytest.raises(ValidationError):
        doc_type_for("photo.jpg")


def test_a_spreadsheet_becomes_one_fact_per_row() -> None:
    data = "Product,Price,In stock\nडीएपी,1250,yes\nयूरिया,266,no\n".encode()
    extracted = extract_file(data, filename="prices.csv")
    assert extracted.doc_type == "csv"
    assert extracted.page_count == 2
    assert "Product: डीएपी · Price: 1250 · In stock: yes" in extracted.markdown
    assert "Product: यूरिया" in extracted.markdown


def test_an_empty_spreadsheet_is_refused_in_the_operators_terms() -> None:
    with pytest.raises(ValidationError) as caught:
        extract_file(b"Product,Price\n", filename="prices.csv")
    assert "column names" in caught.value.remedy


def test_plain_text_is_cleaned_not_rewritten() -> None:
    extracted = extract_file("आलू  में\r\n\r\n\r\nझुलसा".encode(), filename="notes.txt")
    assert extracted.markdown == "आलू में\n\nझुलसा"
    assert extracted.title == "notes"


def test_a_scan_with_no_text_says_so() -> None:
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    with pytest.raises(ValidationError) as caught:
        extract_file(buffer.getvalue(), filename="scan.pdf")
    assert "OCR" in caught.value.remedy


def test_html_keeps_headings_lists_and_tables_and_drops_chrome() -> None:
    page = _Page()
    page.feed(
        """
        <html><head><title>UA Agro — Products</title>
        <script>var x = 1;</script><style>.a{}</style></head>
        <body><nav>Home | Contact</nav>
        <h1>Fertilisers</h1>
        <p>DAP for <b>rabi</b> sowing.</p>
        <ul><li>DAP 50 kg</li><li>Urea 45 kg</li></ul>
        <table><tr><th>Product</th><th>Price</th></tr><tr><td>DAP</td><td>1250</td></tr></table>
        <a href="/products/dap">DAP</a>
        <footer>© UA Agro</footer>
        </body></html>
        """
    )
    text = page.markdown()
    assert page.title == "UA Agro — Products"
    assert "# Fertilisers" in text
    assert "DAP for rabi sowing." in text
    assert "- DAP 50 kg" in text
    assert "| DAP | 1250 |" in text
    assert "var x" not in text
    assert "Home | Contact" not in text
    assert page.links == ["/products/dap"]


def test_a_crawl_stays_on_its_own_site_and_skips_files() -> None:
    base = "https://www.uaagro.in/products"
    assert _same_site(base, "/about") == "https://www.uaagro.in/about"
    assert _same_site(base, "https://www.uaagro.in/x?utm_source=a#top") == "https://www.uaagro.in/x"
    assert _same_site(base, "https://facebook.com/uaagro") is None
    assert _same_site(base, "/brochure.pdf") is None
    assert _same_site(base, "mailto:info@uaagro.in") is None


def test_several_extractions_join_in_order() -> None:
    joined = extract_many([Extracted("# A", "text"), Extracted("# B", "text")])
    assert joined == "# A\n\n# B"
