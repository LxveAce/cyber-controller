"""Tests for the BLE number->name resolvers (src/core/ble_numbers.py) + their analyzer wiring."""

from __future__ import annotations

from src.core.ble_analyzer import BleAnalyzerModel
from src.core.ble_numbers import (
    lookup_appearance,
    lookup_company,
    normalize_company,
    normalize_uuid,
    resolve_uuid,
)


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


# ── appearance ───────────────────────────────────────────────────────────────
def test_lookup_appearance_exact_and_category():
    assert lookup_appearance(962) == "Mouse"            # 0x03C2 exact value
    assert lookup_appearance("0x03C1") == "Keyboard"    # hex form
    assert lookup_appearance(833) == "Heart Rate Belt"  # 0x0341 exact subcategory value
    assert lookup_appearance(832) == "Heart Rate Sensor"  # 0x0340 -> category 13, no exact value
    assert lookup_appearance(64) == "Phone"             # category 1, subcategory 0
    assert lookup_appearance("64") == "Phone"           # decimal string (the LxveOS convention)


def test_lookup_appearance_unknown_and_zero_return_empty():
    assert lookup_appearance(0) == ""            # category 0 "Unknown" -> not labeled
    assert lookup_appearance(None) == ""
    assert lookup_appearance("notanumber") == ""
    assert lookup_appearance(70000) == ""        # beyond 16-bit
    assert lookup_appearance(True) == ""         # bool guard
    assert lookup_appearance(0x1FC0) == ""       # category 127 is unassigned -> no fabricated name


# ── service / member UUIDs ───────────────────────────────────────────────────
def test_resolve_uuid_16bit_forms():
    assert resolve_uuid("180D") == "Heart Rate"
    assert resolve_uuid(0x180F) == "Battery"
    assert resolve_uuid("0x180a") == "Device Information"
    assert resolve_uuid("FEED") == "Tile"          # member (vendor) service UUID
    assert resolve_uuid("fe2c") == "Google Fast Pair"


def test_resolve_uuid_base_128bit_alias():
    # a full 128-bit UUID on the Bluetooth base carries a 16-bit alias in bytes 3-4
    assert resolve_uuid("0000180d-0000-1000-8000-00805f9b34fb") == "Heart Rate"
    assert normalize_uuid("0000180F-0000-1000-8000-00805F9B34FB") == "180F"


def test_resolve_uuid_unknown_and_nonbase_return_empty():
    assert resolve_uuid("1234") == ""     # a well-formed but unassigned 16-bit UUID
    assert resolve_uuid(None) == ""
    assert resolve_uuid("notauuid") == ""
    # a proprietary 128-bit UUID NOT on the Bluetooth base has no 16-bit alias -> never guessed
    assert normalize_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e") is None
    assert resolve_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e") == ""


# ── appearance analyzer wiring ───────────────────────────────────────────────
def test_analyzer_resolves_appearance():
    m = BleAnalyzerModel()
    dev = m.observe({"addr": "aa:bb:cc:dd:ee:02", "appr": 962, "rssi": -60}, now=1.0)
    assert dev is not None
    assert dev.appearance == 962
    assert dev.appearance_name == "Mouse"
    d = dev.to_dict()
    assert d["appearance"] == 962 and d["appearance_name"] == "Mouse"


def test_analyzer_appearance_name_is_sticky_across_readvert():
    m = BleAnalyzerModel()
    m.observe({"addr": "aa:bb:cc:dd:ee:03", "appr": 833, "rssi": -55}, now=1.0)
    # a later plain re-advert with no appr must not blank the classification
    dev = m.observe({"addr": "aa:bb:cc:dd:ee:03", "rssi": -57}, now=2.0)
    assert dev.appearance_name == "Heart Rate Belt"


def test_analyzer_unknown_appearance_keeps_raw_no_fabricated_name():
    m = BleAnalyzerModel()
    # appearance 0 ("Unknown") -> raw value kept, name stays empty (never fabricated)
    dev = m.observe({"addr": "aa:bb:cc:dd:ee:04", "appr": 0}, now=1.0)
    assert dev.appearance == 0
    assert dev.appearance_name == ""
