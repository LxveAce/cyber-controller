"""Meshtastic reference enums — HardwareModel + PortNum names (comms rework, Wave 8 follow-up).

Snapshotted verbatim from the upstream protobuf definitions (meshtastic/protobufs mesh.proto HardwareModel,
portnums.proto PortNum), retrieved 2026-07-23. Field/enum values are fixed by contract, so a static snapshot
is safe; refresh by re-running the enum extractor against the current .proto.

This replaces a small hand-typed hardware-model map that had SIX wrong values (e.g. 71 was labelled RAK4631
when it is TRACKER_T1000_E, 4 was HELTEC_V1 when it is TBEAM) — the exact kind of from-memory error
verify-never-fake exists to catch. Pure data + two pure helpers; no I/O.
"""

from __future__ import annotations

# mesh.proto HardwareModel (145 values).
HARDWARE_MODELS: dict[int, str] = {
    0: "UNSET", 1: "TLORA_V2", 2: "TLORA_V1", 3: "TLORA_V2_1_1P6", 4: "TBEAM", 5: "HELTEC_V2_0",
    6: "TBEAM_V0P7", 7: "T_ECHO", 8: "TLORA_V1_1P3", 9: "RAK4631", 10: "HELTEC_V2_1", 11: "HELTEC_V1",
    12: "LILYGO_TBEAM_S3_CORE", 13: "RAK11200", 14: "NANO_G1", 15: "TLORA_V2_1_1P8", 16: "TLORA_T3_S3",
    17: "NANO_G1_EXPLORER", 18: "NANO_G2_ULTRA", 19: "LORA_TYPE", 20: "WIPHONE", 21: "WIO_WM1110",
    22: "RAK2560", 23: "HELTEC_HRU_3601", 24: "HELTEC_WIRELESS_BRIDGE", 25: "STATION_G1", 26: "RAK11310",
    27: "MAKERFABS_TRACKER", 28: "MAKERFABS_RESERVED", 29: "CANARYONE", 30: "RP2040_LORA", 31: "STATION_G2",
    32: "LORA_RELAY_V1", 33: "T_ECHO_PLUS", 34: "PPR", 35: "GENIEBLOCKS", 36: "NRF52_UNKNOWN", 37: "PORTDUINO",
    38: "ANDROID_SIM", 39: "DIY_V1", 40: "NRF52840_PCA10059", 41: "DR_DEV", 42: "M5STACK", 43: "HELTEC_V3",
    44: "HELTEC_WSL_V3", 45: "BETAFPV_2400_TX", 46: "BETAFPV_900_NANO_TX", 47: "RPI_PICO",
    48: "HELTEC_WIRELESS_TRACKER", 49: "HELTEC_WIRELESS_PAPER", 50: "T_DECK", 51: "T_WATCH_S3",
    52: "PICOMPUTER_S3", 53: "HELTEC_HT62", 54: "EBYTE_ESP32_S3", 55: "ESP32_S3_PICO", 56: "CHATTER_2",
    57: "HELTEC_WIRELESS_PAPER_V1_0", 58: "HELTEC_WIRELESS_TRACKER_V1_0", 59: "UNPHONE", 60: "TD_LORAC",
    61: "CDEBYTE_EORA_S3", 62: "TWC_MESH_V4", 63: "NRF52_PROMICRO_DIY", 64: "RADIOMASTER_900_BANDIT_NANO",
    65: "HELTEC_CAPSULE_SENSOR_V3", 66: "HELTEC_VISION_MASTER_T190", 67: "HELTEC_VISION_MASTER_E213",
    68: "HELTEC_VISION_MASTER_E290", 69: "HELTEC_MESH_NODE_T114", 70: "SENSECAP_INDICATOR",
    71: "TRACKER_T1000_E", 72: "RAK3172", 73: "WIO_E5", 74: "RADIOMASTER_900_BANDIT", 75: "ME25LS01_4Y10TD",
    76: "RP2040_FEATHER_RFM95", 77: "M5STACK_COREBASIC", 78: "M5STACK_CORE2", 79: "RPI_PICO2",
    80: "M5STACK_CORES3", 81: "SEEED_XIAO_S3", 82: "MS24SF1", 83: "TLORA_C6", 84: "WISMESH_TAP",
    85: "ROUTASTIC", 86: "MESH_TAB", 87: "MESHLINK", 88: "XIAO_NRF52_KIT", 89: "THINKNODE_M1",
    90: "THINKNODE_M2", 91: "T_ETH_ELITE", 92: "HELTEC_SENSOR_HUB", 93: "MUZI_BASE", 94: "HELTEC_MESH_POCKET",
    95: "SEEED_SOLAR_NODE", 96: "NOMADSTAR_METEOR_PRO", 97: "CROWPANEL", 98: "LINK_32",
    99: "SEEED_WIO_TRACKER_L1", 100: "SEEED_WIO_TRACKER_L1_EINK", 101: "MUZI_R1_NEO", 102: "T_DECK_PRO",
    103: "T_LORA_PAGER", 104: "M5STACK_RESERVED", 105: "WISMESH_TAG", 106: "RAK3312", 107: "THINKNODE_M5",
    108: "HELTEC_MESH_SOLAR", 109: "T_ECHO_LITE", 110: "HELTEC_V4", 111: "M5STACK_C6L",
    112: "M5STACK_CARDPUTER_ADV", 113: "HELTEC_WIRELESS_TRACKER_V2", 114: "T_WATCH_ULTRA", 115: "THINKNODE_M3",
    116: "WISMESH_TAP_V2", 117: "RAK3401", 118: "RAK6421", 119: "THINKNODE_M4", 120: "THINKNODE_M6",
    121: "MESHSTICK_1262", 122: "TBEAM_1_WATT", 123: "T5_S3_EPAPER_PRO", 124: "TBEAM_BPF",
    125: "MINI_EPAPER_S3", 126: "TDISPLAY_S3_PRO", 127: "HELTEC_MESH_NODE_T096", 128: "MESH_TRACKER_X1",
    129: "THINKNODE_M7", 130: "THINKNODE_M8", 131: "THINKNODE_M9", 132: "HELTEC_V4_R8",
    133: "HELTEC_MESH_NODE_T1", 134: "STATION_G3", 135: "T_IMPULSE_PLUS", 136: "T_ECHO_CARD",
    137: "SEEED_WIO_TRACKER_L2", 138: "CROWPANEL_P4", 139: "HELTEC_MESH_TOWER_V2", 140: "MESHNOLOGY_W10",
    141: "HELTEC_RC32", 142: "HELTEC_RC52", 143: "HELTEC_RCC6", 255: "PRIVATE_HW",
}

# portnums.proto PortNum (40 values).
PORTNUMS: dict[int, str] = {
    0: "UNKNOWN_APP", 1: "TEXT_MESSAGE_APP", 2: "REMOTE_HARDWARE_APP", 3: "POSITION_APP", 4: "NODEINFO_APP",
    5: "ROUTING_APP", 6: "ADMIN_APP", 7: "TEXT_MESSAGE_COMPRESSED_APP", 8: "WAYPOINT_APP", 9: "AUDIO_APP",
    10: "DETECTION_SENSOR_APP", 11: "ALERT_APP", 12: "KEY_VERIFICATION_APP", 13: "REMOTE_SHELL_APP",
    32: "REPLY_APP", 33: "IP_TUNNEL_APP", 34: "PAXCOUNTER_APP", 35: "STORE_FORWARD_PLUSPLUS_APP",
    36: "NODE_STATUS_APP", 37: "MESH_BEACON_APP", 64: "SERIAL_APP", 65: "STORE_FORWARD_APP",
    66: "RANGE_TEST_APP", 67: "TELEMETRY_APP", 68: "ZPS_APP", 69: "SIMULATOR_APP", 70: "TRACEROUTE_APP",
    71: "NEIGHBORINFO_APP", 72: "ATAK_PLUGIN", 73: "MAP_REPORT_APP", 74: "POWERSTRESS_APP",
    75: "LORAWAN_BRIDGE", 76: "RETICULUM_TUNNEL_APP", 77: "CAYENNE_APP", 78: "ATAK_PLUGIN_V2",
    79: "LORA_OTA_APP", 112: "GROUPALARM_APP", 256: "PRIVATE_APP", 257: "ATAK_FORWARDER", 511: "MAX",
}


def hardware_model_name(code: int | None) -> str:
    """Human name for a Meshtastic HardwareModel value, or ``"hw#N"`` for one not in the snapshot."""
    if code is None:
        return ""
    return HARDWARE_MODELS.get(code, f"hw#{code}")


def portnum_name(code: int | None) -> str:
    """Human name for a Meshtastic PortNum value, or ``"portnum#N"`` for one not in the snapshot."""
    if code is None:
        return ""
    return PORTNUMS.get(code, f"portnum#{code}")
