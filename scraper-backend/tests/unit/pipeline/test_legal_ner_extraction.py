import pytest

from pipeline.legal_ner_extraction import _section_numbers


@pytest.mark.parametrize("raw, expected", [
    ("Section 302", ["302"]),
    ("Article 21", ["21"]),
    ("section-302", ["302"]),
    ("s. 2(1)(d)", ["2(1)(d)"]),
    ("302/34", ["302", "34"]),
    ("308,34", ["308", "34"]),
    ("Section 138 r/w 141", ["138", "141"]),
    ("103/3(5", ["103", "3(5)"]),
    ("108 alternatively 103/3(5", ["108", "103", "3(5)"]),
    ("181(2)(r) and (s)", ["181(2)(r)", "181(2)(s)"]),
    ("sub-section (2) of Section 56", ["56(2)"]),
    ("Regulation 4 (a)", ["4(a)"]),
    ("Regulation 4(2)(a) to (e)]", ["4(2)(a)", "4(2)(e)"]),
    ("85 of BNS", ["85"]),
    ("319CrPC", ["319"]),
    ("304 Part I/II, or cases of 4 CRA-1219-2018", ["304"]),
    ("113-A", ["113-A"]),
    ("12AA", ["12AA"]),
    ("120B", ["120B"]),
    ("2023", []),
    ("Order XXXIX Rule 1", []),
])
def test_section_numbers(raw, expected):
    assert _section_numbers(raw) == expected
