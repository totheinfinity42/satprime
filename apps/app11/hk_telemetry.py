import struct
import zlib
import lzma
import hashlib
import hmac
import logging
import concurrent.futures
import statistics
import collections
import array
import datetime
import json
import ssl
import xml.etree.ElementTree as ET
import asyncio

LOG = logging.getLogger("ccsds.hk")

_PRI_FMT   = ">HHH"
_PRI_SIZE  = struct.calcsize(_PRI_FMT)
_SEC_FMT   = ">IHH"
_SEC_SIZE  = struct.calcsize(_SEC_FMT)
_CRC_POLY  = 0x1021
_CRC_INIT  = 0xFFFF

APID_POWER   = 0x100
APID_THERMAL = 0x101
APID_ADCS    = 0x102
APID_SCIENCE = 0x200

_COMP_ZLIB = 0x01
_COMP_LZMA = 0x02

_GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)
_GPS_LEAP  = 18
_HMAC_KEY  = bytes.fromhex("73A491F2BC8E053D4A2C9F01E7B6D820")

_CAL_POWER = {
    0x01: ('L', (0.0,    33.0)),
    0x02: ('Q', (-15.0,  30.0,  -3.6)),
    0x03: ('P', (-273.15, 412.3, -3.12, 8.7e-3, -8.1e-6)),
    0x04: ('L', (0.0,   150.0)),
    0x05: ('L', (-5.0,   10.0)),
}
_CAL_THERMAL = {
    0x10: ('P', (-273.15, 410.1, -2.98, 8.1e-3, -7.4e-6)),
    0x11: ('P', (-273.15, 410.1, -2.98, 8.1e-3, -7.4e-6)),
    0x12: ('P', (-273.15, 408.7, -3.01, 8.3e-3, -7.9e-6)),
    0x13: ('L', (0.0, 1.0)),
    0x14: ('L', (0.0, 1.0)),
}
_CAL_ADCS = {
    0x20: ('Q', (0.0,  500.0, -0.02)),
    0x21: ('Q', (0.0,  500.0, -0.02)),
    0x22: ('Q', (0.0,  500.0, -0.02)),
    0x23: ('L', (-6000.0, 12000.0 / 4095)),
    0x24: ('L', (-6000.0, 12000.0 / 4095)),
    0x25: ('L', (-6000.0, 12000.0 / 4095)),
}
_CAL_MAP = {APID_POWER: _CAL_POWER, APID_THERMAL: _CAL_THERMAL, APID_ADCS: _CAL_ADCS}

_LIMITS = {
    0x01: (22.0,  34.0),   0x02: (-12.0, 12.0),   0x03: (-40.0, 65.0),
    0x10: (-60.0, 80.0),   0x11: (-60.0, 80.0),   0x12: (-30.0, 70.0),
    0x20: (-20.0, 20.0),   0x21: (-20.0, 20.0),   0x22: (-20.0, 20.0),
}

HKChannel = collections.namedtuple("HKChannel", ["id", "raw", "eu", "anomaly"])
HKPacket  = collections.namedtuple("HKPacket",  ["apid", "seq", "ts", "utc", "channels", "ok"])

_WER_WIN  = 9
_ch_hist: dict[int, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=_WER_WIN)
)
_anom_cnt: collections.Counter = collections.Counter()


def _crc16(data: bytes) -> int:
    crc = _CRC_INIT
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC_POLY) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


def _gps_to_utc(gps_s: float) -> datetime.datetime:
    return (_GPS_EPOCH + datetime.timedelta(seconds=gps_s - _GPS_LEAP)
            ).replace(tzinfo=datetime.timezone.utc)


def _hmac_tag(data: bytes) -> bytes:
    return hmac.new(_HMAC_KEY, data, hashlib.sha256).digest()[:8]


def _calibrate(raw: int, cal: tuple) -> float:
    kind, c = cal
    x = raw / 4095.0
    if kind == 'L':
        return c[0] + c[1] * x
    if kind == 'Q':
        return c[0] + c[1] * x + c[2] * x * x
    v = c[-1]
    for coeff in reversed(c[:-1]):
        v = v * x + coeff
    return v


def _wer_check(ch_id: int, z: float) -> list[int]:
    hist = list(_ch_hist[ch_id])
    triggered = []
    if abs(z) > 3.0:
        triggered.append(1)
    w3 = hist[-2:] + [z]
    if len(w3) == 3:
        if sum(v > 2.0 for v in w3) >= 2 or sum(v < -2.0 for v in w3) >= 2:
            triggered.append(2)
    w5 = hist[-4:] + [z]
    if len(w5) == 5:
        if sum(v > 1.0 for v in w5) >= 4 or sum(v < -1.0 for v in w5) >= 4:
            triggered.append(3)
    w8 = hist[-7:] + [z]
    if len(w8) == 8:
        if all(v > 0.0 for v in w8) or all(v < 0.0 for v in w8):
            triggered.append(4)
    return triggered


def _wer_update(ch_id: int, eu: float) -> tuple[float, list[int]]:
    hist = _ch_hist[ch_id]
    if len(hist) < 3:
        hist.append(eu)
        return 0.0, []
    mu  = statistics.mean(hist)
    sig = statistics.stdev(hist)
    z   = (eu - mu) / (sig + 1e-12)
    rules = _wer_check(ch_id, z)
    hist.append(eu)
    return z, rules


def _pri(buf: bytes) -> dict:
    w0, w1, w2 = struct.unpack_from(_PRI_FMT, buf)
    return {"apid": w0 & 0x07FF, "seq": w1 & 0x3FFF,
            "shf": (w0 >> 11) & 1, "dlen": w2}


def _sec(buf: bytes) -> float:
    coarse, fine, _ = struct.unpack_from(_SEC_FMT, buf, _PRI_SIZE)
    return coarse + fine / 65536.0


def decode_hk(buf: bytes) -> HKPacket:
    _BAD = HKPacket(0, 0, 0.0, None, [], False)
    if len(buf) < _PRI_SIZE + _SEC_SIZE + 3:
        return _BAD
    stored = struct.unpack_from(">H", buf, len(buf) - 2)[0]
    if _crc16(buf[:-2]) != stored:
        LOG.warning("CRC mismatch apid=0x%03X stored=%04X computed=%04X",
                    _pri(buf)["apid"], stored, _crc16(buf[:-2]))
        return _BAD
    ph  = _pri(buf)
    ts  = _sec(buf)
    utc = _gps_to_utc(ts)
    off = _PRI_SIZE + _SEC_SIZE
    cal = _CAL_MAP.get(ph["apid"], {})
    n   = buf[off]; off += 1
    channels = []
    for _ in range(n):
        if off + 3 > len(buf) - 2:
            break
        ch_id, raw = struct.unpack_from(">BH", buf, off); off += 3
        eu = _calibrate(raw, cal[ch_id]) if ch_id in cal else float(raw)
        z, rules = _wer_update(ch_id, eu)
        lim = _LIMITS.get(ch_id)
        bad = bool(rules or (lim and not lim[0] <= eu <= lim[1]))
        if bad:
            _anom_cnt[ch_id] += 1
            LOG.warning("ANOMALY ch=0x%02X eu=%.3f z=%.2f rules=%s",
                        ch_id, eu, z, rules)
        channels.append(HKChannel(ch_id, raw, round(eu, 4), bad))
    return HKPacket(ph["apid"], ph["seq"], ts, utc, channels, True)


def decode_science(buf: bytes) -> tuple[array.array, array.array] | None:
    off     = _PRI_SIZE + _SEC_SIZE
    comp    = buf[off]
    payload = buf[off + 1 : len(buf) - 4]
    stored  = struct.unpack_from(">I", buf, len(buf) - 4)[0]
    if zlib.crc32(payload) & 0xFFFFFFFF != stored:
        LOG.error("Science frame CRC-32 mismatch")
        return None
    if comp == _COMP_ZLIB:
        raw = zlib.decompress(payload)
    elif comp == _COMP_LZMA:
        raw = lzma.decompress(payload)
    else:
        LOG.error("Unknown compression tag 0x%02X", comp)
        return None
    n     = len(raw) // 4
    I_arr = array.array('h', raw[:n * 2]);      I_arr.byteswap()
    Q_arr = array.array('h', raw[n * 2:n * 4]); Q_arr.byteswap()
    LOG.info("Science frame: %d IQ sample pairs", n)
    return I_arr, Q_arr


def _dispatch(pkt_bytes: bytes) -> HKPacket | None:
    if _pri(pkt_bytes)["apid"] == APID_SCIENCE:
        iq = decode_science(pkt_bytes)
        LOG.info("Science frame: %d IQ pairs", len(iq[0]) if iq else -1)
        return None
    return decode_hk(pkt_bytes)


def process_batch(raw_pkts: list[bytes]) -> list[HKPacket]:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_dispatch, p): i for i, p in enumerate(raw_pkts)}
        for fut in concurrent.futures.as_completed(futs):
            pkt = fut.result()
            if pkt and pkt.ok:
                results.append(pkt)
    return sorted(results, key=lambda p: p.ts)


def build_report(packets: list[HKPacket]) -> str:
    root = ET.Element("TelemetryReport")
    summary = ET.SubElement(root, "Summary")
    ET.SubElement(summary, "PacketCount").text     = str(len(packets))
    ET.SubElement(summary, "AnomalyChannels").text = str(len(_anom_cnt))
    blob = b"".join(struct.pack(">Id", p.seq, p.ts) for p in packets)
    ET.SubElement(summary, "BatchDigest").text = hashlib.sha256(blob).hexdigest()[:16]
    for pkt in packets:
        pe = ET.SubElement(root, "Packet",
                           apid=f"0x{pkt.apid:03X}",
                           seq=str(pkt.seq),
                           ts=f"{pkt.ts:.3f}",
                           utc=pkt.utc.isoformat() if pkt.utc else "")
        for ch in pkt.channels:
            ET.SubElement(pe, "Ch",
                          id=f"0x{ch.id:02X}",
                          raw=str(ch.raw),
                          eu=f"{ch.eu:.4f}",
                          anom=str(ch.anomaly))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def build_json_report(packets: list[HKPacket]) -> str:
    blob = b"".join(struct.pack(">Id", p.seq, p.ts) for p in packets)
    return json.dumps({
        "batch_digest": hashlib.sha256(blob).hexdigest()[:16],
        "packets": [
            {"apid":     f"0x{p.apid:03X}",
             "seq":      p.seq,
             "ts_gps":   p.ts,
             "utc":      p.utc.isoformat() if p.utc else None,
             "channels": [{"id":  f"0x{ch.id:02X}", "eu": ch.eu,
                           "raw": ch.raw, "anom": ch.anomaly}
                          for ch in p.channels]}
            for p in packets
        ],
        "anomaly_summary": dict(_anom_cnt),
    }, indent=2)


async def _send(report: str, host: str = "gs.local", port: int = 9900) -> None:
    ctx               = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode   = ssl.CERT_NONE
    frame = report.encode()
    tag   = _hmac_tag(frame)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout=5.0
        )
        writer.write(struct.pack(">I8s", len(frame), tag) + frame)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        LOG.info("Report dispatched to %s:%d (%d bytes)", host, port, len(frame))
    except (OSError, asyncio.TimeoutError) as exc:
        LOG.warning("Ground station unreachable (%s) — stored locally", exc)


async def pipeline(raw_pkts: list[bytes]) -> dict[str, str]:
    hk      = process_batch(raw_pkts)
    xml_rep = build_report(hk)
    jsn_rep = build_json_report(hk)
    await _send(xml_rep)
    return {"xml": xml_rep, "json": jsn_rep}


def _make_hk(apid: int, seq: int, ts: float, channels: list[tuple]) -> bytes:
    coarse = int(ts)
    fine   = int((ts - coarse) * 65536)
    sec    = struct.pack(_SEC_FMT, coarse, fine, 0)
    user   = bytes([len(channels)])
    for ch_id, raw in channels:
        user += struct.pack(">BH", ch_id, raw)
    dlen = len(sec) + len(user) + 2 - 1
    w0   = (1 << 11) | (apid & 0x07FF)
    w1   = (0b11 << 14) | (seq & 0x3FFF)
    hdr  = struct.pack(_PRI_FMT, w0, w1, dlen)
    body = hdr + sec + user
    return body + struct.pack(">H", _crc16(body))


def _make_science(seq: int, ts: float) -> bytes:
    seed = struct.pack(">Id", seq, ts)
    raw  = b""
    while len(raw) < 2048:
        seed = hashlib.sha256(seed).digest()
        raw += seed
    raw = raw[:2048]
    compressed = lzma.compress(raw, preset=3)
    crc32      = zlib.crc32(compressed) & 0xFFFFFFFF
    coarse     = int(ts)
    fine       = int((ts - coarse) * 65536)
    sec        = struct.pack(_SEC_FMT, coarse, fine, 0)
    user       = bytes([_COMP_LZMA]) + compressed + struct.pack(">I", crc32)
    dlen       = len(sec) + len(user) - 1
    w0         = (1 << 11) | (APID_SCIENCE & 0x07FF)
    w1         = (0b11 << 14) | (seq & 0x3FFF)
    hdr        = struct.pack(_PRI_FMT, w0, w1, dlen)
    return hdr + sec + user


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    BASE_TS = 757_382_400.0

    def _lcg(s: int, lo: int, hi: int) -> tuple[int, int]:
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        return lo + s % (hi - lo + 1), s

    pkts: list[bytes] = []
    seed = 0xDEADBEEF

    for i in range(8):
        v1, seed = _lcg(seed, 2730, 4095)
        v2, seed = _lcg(seed, 1500, 2600)
        v3, seed = _lcg(seed, 1600, 2500)
        pkts.append(_make_hk(APID_POWER, i, BASE_TS + i * 0.5,
                             [(0x01, v1), (0x02, v2), (0x03, v3)]))

    for i in range(8):
        v1, seed = _lcg(seed, 1400, 2800)
        v2, seed = _lcg(seed, 1400, 2800)
        v3, seed = _lcg(seed, 1600, 2400)
        pkts.append(_make_hk(APID_THERMAL, i, BASE_TS + i * 0.5 + 0.1,
                             [(0x10, v1), (0x11, v2), (0x12, v3)]))

    for i in range(6):
        v1, seed = _lcg(seed, 1900, 2200)
        v2, seed = _lcg(seed, 1900, 2200)
        v3, seed = _lcg(seed, 1900, 2200)
        pkts.append(_make_hk(APID_ADCS, i, BASE_TS + i * 0.5 + 0.2,
                             [(0x20, v1), (0x21, v2), (0x22, v3)]))

    pkts.append(_make_science(0, BASE_TS + 3.0))

    reports = asyncio.run(pipeline(pkts))
    print(reports["xml"][:2000])
    print(f"\n... ({len(reports['xml'])} chars XML)")
    print("\n--- JSON (first 600 chars) ---")
    print(reports["json"][:600])
    print(f"\nAnomaly summary: {dict(_anom_cnt)}")
