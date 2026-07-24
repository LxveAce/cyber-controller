"""Tests for the BLE company-id -> vendor resolver (src/core/ble_numbers.py) + its analyzer wiring."""

from __future__ import annotations

from src.core.ble_analyzer import BleAnalyzerModel
from src.core.ble_numbers import lookup_company, normalize_company


def test_lookup_known_companies_decimal():
    # LxveOS emits company=<decimal> on its LXVEOS/1 ble line (parsed there as int(val)).
    assert lookup_company("76") == "Apple, Inc."                 # 0x004C
    assert lookup_company("6") == "Microsoft"                    # 0x0006
    assert lookup_company("224") == "Google"                     # 0x00E0
    assert lookup_company("89") == "Nordic Semiconductor ASA"    # 0x0059


def test_lookup_int_and_hex_forms():
    assert lookup_company(76) == "Apple, Inc."
    assert lookup_company("0x004c") == "Apple, Inc."
    assert lookup_company("0x4C") == "Apple, Inc."


def test_decimal_is_not_misread_as_hex():
    # "76" must be decimal (Apple 0x004C), never hex 0x76 (118) — a misread would mislabel the vendor.
    assert normalize_company("76") == "004C"
    assert lookup_company("76") != lookup_company("0x76")


def test_bad_and_out_of_range_inputs_return_empty():
    assert lookup_company("") == ""
    assert lookup_company(None) == ""
    assert lookup_company("notanumber") == ""
    assert lookup_company(-1) == ""
    assert lookup_company(70000) == ""     # beyond 16-bit
    assert lookup_company(True) == ""      # bool guard (bool is an int subclass)
    assert normalize_company(True) is None


def test_normalize_forms():
    assert normalize_company(76) == "004C"
    assert normalize_company("004c") == "004C"
    assert normalize_company("0x4c") == "004C"
    assert normalize_company("4c") == "004C"  # bare hex-lettered string


def test_analyzer_resolves_company_name():
    m = BleAnalyzerModel()
    dev = m.observe({"addr": "aa:bb:cc:dd:ee:ff", "company": 76, "rssi": -50}, now=1.0)
    assert dev is not None
    assert dev.company == "76"
    assert dev.company_name == "Apple, Inc."
    assert dev.to_dict()["company_name"] == "Apple, Inc."


def test_analyzer_unknown_company_keeps_raw_no_fabricated_name():
    m = BleAnalyzerModel()
    # a company id with no assigned vendor -> raw id kept, name stays empty (never fabricated)
    dev = m.observe({"addr": "aa:bb:cc:dd:ee:01", "company": "notanumber"}, now=1.0)
    assert dev.company == "notanumber"
    assert dev.company_name == ""
