"""TargetIngestor callback lifecycle (#11 / #20): a re-attach on the SAME (co-owned) connection must
not stack a second on_line callback — otherwise every serial line is parsed and pooled twice, and the
callback leaks. Pure logic, no Qt / hardware."""

from __future__ import annotations


class _FakeConn:
    def __init__(self, port):
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def remove_line_callback(self, cb):
        try:
            self._cbs.remove(cb)
        except ValueError:
            pass

    def emit(self, line):
        for cb in list(self._cbs):
            cb(line)


class _FakePool:
    def __init__(self):
        self.adds = 0

    def add(self, _t):
        self.adds += 1


class _Ev:
    event_type = "ap_found"
    data = {"bssid": "AA:BB:CC:DD:EE:FF", "ssid": "x"}


class _FakeProto:
    def parse_line(self, _line):
        return _Ev()


def test_reattach_on_shared_conn_is_idempotent():
    from src.core.target_ingest import TargetIngestor

    pool = _FakePool()
    ing = TargetIngestor(pool)
    conn = _FakeConn("COM7")  # a shared conn a co-owner keeps alive across a devices-tab reconnect

    ing.attach(conn, _FakeProto())   # devices-tab connect
    ing.attach(conn, _FakeProto())   # reconnect on the SAME live conn (co-owner still held it)
    conn.emit("some scan line")

    assert pool.adds == 1, "exactly one ingest per line, not two"
    assert len(conn._cbs) == 1, "no orphaned callback left stacked"


def test_attach_detach_attach_no_stack():
    from src.core.target_ingest import TargetIngestor

    pool = _FakePool()
    ing = TargetIngestor(pool)
    conn = _FakeConn("COM7")

    ing.attach(conn, _FakeProto())
    ing.detach(conn)
    ing.attach(conn, _FakeProto())
    conn.emit("line")

    assert pool.adds == 1 and len(conn._cbs) == 1
