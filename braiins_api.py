"""Pure-stdlib Braiins OS+ Public API client (gRPC over HTTP/2 h2c + REST fallback).

Implements the documented Braiins OS+ Public API (`braiins.bos.v1` on TCP 50051
and `/api/v1/...` REST on HTTP 80) without external `grpcio` or `protobuf`
packages so `MinerHearth.exe` and `run.bat` remain dependency-free.
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
import urllib.error
import urllib.request
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Protobuf wire-format codec (proto3 subset needed for braiins.bos.v1)
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def decode_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(buf):
        b = buf[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, offset
        shift += 7
        if shift >= 70:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def encode_field_varint(field_num: int, value: int) -> bytes:
    return encode_varint((field_num << 3) | 0) + encode_varint(value)


def encode_field_bytes(field_num: int, data: bytes) -> bytes:
    return encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data


def encode_field_str(field_num: int, text: str) -> bytes:
    return encode_field_bytes(field_num, text.encode("utf-8"))


def parse_proto_fields(buf: bytes) -> dict[int, list[tuple[int, Any]]]:
    fields: dict[int, list[tuple[int, Any]]] = {}
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(buf, pos)
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("truncated 64-bit field")
            val = buf[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = decode_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            val = buf[pos : pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("truncated 32-bit field")
            val = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_num, []).append((wire_type, val))
    return fields


def _first_varint(fields: dict[int, list[tuple[int, Any]]], field_num: int, default: int = 0) -> int:
    for wire, val in fields.get(field_num, []):
        if wire == 0:
            return int(val)
    return default


def _first_bytes(fields: dict[int, list[tuple[int, Any]]], field_num: int) -> Optional[bytes]:
    for wire, val in fields.get(field_num, []):
        if wire == 2:
            return bytes(val)
    return None


def _first_str(fields: dict[int, list[tuple[int, Any]]], field_num: int, default: str = "") -> str:
    raw = _first_bytes(fields, field_num)
    if raw is None:
        return default
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# RFC 7541 HPACK static table + Huffman decoder
# ---------------------------------------------------------------------------

_HPACK_STATIC: list[tuple[str, str]] = [
    ("", ""),  # 0 unused
    (":authority", ""),
    (":method", "GET"),
    (":method", "POST"),
    (":path", "/"),
    (":path", "/index.html"),
    (":scheme", "http"),
    (":scheme", "https"),
    (":status", "200"),
    (":status", "204"),
    (":status", "206"),
    (":status", "304"),
    (":status", "400"),
    (":status", "404"),
    (":status", "500"),
    ("accept-charset", ""),
    ("accept-encoding", "gzip, deflate"),
    ("accept-language", ""),
    ("accept-ranges", ""),
    ("accept", ""),
    ("access-control-allow-origin", ""),
    ("age", ""),
    ("allow", ""),
    ("authorization", ""),
    ("cache-control", ""),
    ("content-disposition", ""),
    ("content-encoding", ""),
    ("content-language", ""),
    ("content-length", ""),
    ("content-location", ""),
    ("content-range", ""),
    ("content-type", ""),
    ("cookie", ""),
    ("date", ""),
    ("etag", ""),
    ("expect", ""),
    ("expires", ""),
    ("from", ""),
    ("host", ""),
    ("if-match", ""),
    ("if-modified-since", ""),
    ("if-none-match", ""),
    ("if-range", ""),
    ("if-unmodified-since", ""),
    ("last-modified", ""),
    ("link", ""),
    ("location", ""),
    ("max-forwards", ""),
    ("proxy-authenticate", ""),
    ("proxy-authorization", ""),
    ("range", ""),
    ("referer", ""),
    ("refresh", ""),
    ("retry-after", ""),
    ("server", ""),
    ("set-cookie", ""),
    ("strict-transport-security", ""),
    ("transfer-encoding", ""),
    ("user-agent", ""),
    ("vary", ""),
    ("via", ""),
    ("www-authenticate", ""),
]

# RFC 7541 Appendix B Huffman codes: (code, bit_length) for symbols 0..256 (256=EOS)
_HUFFMAN_CODES: list[tuple[int, int]] = [
    (0x1FF8, 13), (0x7FFFD8, 23), (0xFFFFFE2, 28), (0xFFFFFE3, 28),
    (0xFFFFFE4, 28), (0xFFFFFE5, 28), (0xFFFFFE6, 28), (0xFFFFFE7, 28),
    (0xFFFFFE8, 28), (0xFFFFEA, 24), (0x3FFFFFFC, 30), (0xFFFFFE9, 28),
    (0xFFFFFEA, 28), (0x3FFFFFFD, 30), (0xFFFFFEB, 28), (0xFFFFFEC, 28),
    (0xFFFFFED, 28), (0xFFFFFEE, 28), (0xFFFFFEF, 28), (0xFFFFFF0, 28),
    (0xFFFFFF1, 28), (0xFFFFFF2, 28), (0x3FFFFFFE, 30), (0xFFFFFF3, 28),
    (0xFFFFFF4, 28), (0xFFFFFF5, 28), (0xFFFFFF6, 28), (0xFFFFFF7, 28),
    (0xFFFFFF8, 28), (0xFFFFFF9, 28), (0xFFFFFFA, 28), (0xFFFFFFB, 28),
    (0x14, 6), (0x3F8, 10), (0x3F9, 10), (0xFFA, 12),
    (0x1FF9, 13), (0x15, 6), (0xF8, 8), (0x7FA, 11),
    (0x3FA, 10), (0x3FB, 10), (0xF9, 8), (0x7FB, 11),
    (0xFA, 8), (0x16, 6), (0x17, 6), (0x18, 6),
    (0x0, 5), (0x1, 5), (0x2, 5), (0x19, 6),
    (0x1A, 6), (0x1B, 6), (0x1C, 6), (0x1D, 6),
    (0x1E, 6), (0x1F, 6), (0x5C, 7), (0xFB, 8),
    (0x7FFC, 15), (0x20, 6), (0xFFB, 12), (0x3FC, 10),
    (0x1FFA, 13), (0x21, 6), (0x5D, 7), (0x5E, 7),
    (0x5F, 7), (0x60, 7), (0x61, 7), (0x62, 7),
    (0x63, 7), (0x64, 7), (0x65, 7), (0x66, 7),
    (0x67, 7), (0x68, 7), (0x69, 7), (0x6A, 7),
    (0x6B, 7), (0x6C, 7), (0x6D, 7), (0x6E, 7),
    (0x6F, 7), (0x70, 7), (0x71, 7), (0x72, 7),
    (0xFC, 8), (0x73, 7), (0xFD, 8), (0x1FFB, 13),
    (0x7FFF0, 19), (0x1FFC, 13), (0x3FFC, 14), (0x22, 6),
    (0x7FFD, 15), (0x3, 5), (0x23, 6), (0x4, 5),
    (0x24, 6), (0x5, 5), (0x25, 6), (0x26, 6),
    (0x27, 6), (0x6, 5), (0x74, 7), (0x75, 7),
    (0x28, 6), (0x29, 6), (0x2A, 6), (0x7, 5),
    (0x2B, 6), (0x76, 7), (0x2C, 6), (0x8, 5),
    (0x9, 5), (0x2D, 6), (0x77, 7), (0x78, 7),
    (0x79, 7), (0x7A, 7), (0x7B, 7), (0x7FFE, 15),
    (0x7FC, 11), (0x3FFD, 14), (0x1FFD, 13), (0xFFFFFFC, 28),
    (0xFFFE6, 20), (0x3FFFD2, 22), (0xFFFE7, 20), (0xFFFE8, 20),
    (0x3FFFD3, 22), (0x3FFFD4, 22), (0x3FFFD5, 22), (0x7FFFD9, 23),
    (0x3FFFD6, 22), (0x7FFFDA, 23), (0x7FFFDB, 23), (0x7FFFDC, 23),
    (0x7FFFDD, 23), (0x7FFFDE, 23), (0xFFFFEB, 24), (0x7FFFDF, 23),
    (0xFFFFEC, 24), (0xFFFFED, 24), (0x3FFFD7, 22), (0x7FFFE0, 23),
    (0xFFFFEE, 24), (0x7FFFE1, 23), (0x7FFFE2, 23), (0x7FFFE3, 23),
    (0x7FFFE4, 23), (0x1FFFDC, 21), (0x3FFFD8, 22), (0x7FFFE5, 23),
    (0x3FFFD9, 22), (0x7FFFE6, 23), (0x7FFFE7, 23), (0xFFFFEF, 24),
    (0x3FFFDA, 22), (0x1FFFDD, 21), (0xFFFE9, 20), (0x3FFFDB, 22),
    (0x3FFFDC, 22), (0x7FFFE8, 23), (0x7FFFE9, 23), (0x1FFFDE, 21),
    (0x7FFFEA, 23), (0x3FFFDD, 22), (0x3FFFDE, 22), (0xFFFFF0, 24),
    (0x1FFFDF, 21), (0x3FFFDF, 22), (0x7FFFEB, 23), (0x7FFFEC, 23),
    (0x1FFFE0, 21), (0x1FFFE1, 21), (0x3FFFE0, 22), (0x1FFFE2, 21),
    (0x7FFFED, 23), (0x3FFFE1, 22), (0x7FFFEE, 23), (0x7FFFEF, 23),
    (0xFFFEA, 20), (0x3FFFE2, 22), (0x3FFFE3, 22), (0x3FFFE4, 22),
    (0x7FFFF0, 23), (0x3FFFE5, 22), (0x3FFFE6, 22), (0x7FFFF1, 23),
    (0x3FFFFE0, 26), (0x3FFFFE1, 26), (0xFFFEB, 20), (0x7FFF1, 19),
    (0x3FFFE7, 22), (0x7FFFF2, 23), (0x3FFFE8, 22), (0x1FFFFEC, 25),
    (0x3FFFFE2, 26), (0x3FFFFE3, 26), (0x3FFFFE4, 26), (0x7FFFFDE, 27),
    (0x7FFFFDF, 27), (0x3FFFFE5, 26), (0xFFFFF1, 24), (0x1FFFFED, 25),
    (0x7FFF2, 19), (0x1FFFE3, 21), (0x3FFFFE6, 26), (0x7FFFFE0, 27),
    (0x7FFFFE1, 27), (0x3FFFFE7, 26), (0x7FFFFE2, 27), (0xFFFFF2, 24),
    (0x1FFFE4, 21), (0x1FFFE5, 21), (0x3FFFFE8, 26), (0x3FFFFE9, 26),
    (0xFFFFFFD, 28), (0x7FFFFE3, 27), (0x7FFFFE4, 27), (0x7FFFFE5, 27),
    (0xFFFEC, 20), (0xFFFFF3, 24), (0xFFFED, 20), (0x1FFFE6, 21),
    (0x3FFFE9, 22), (0x1FFFE7, 21), (0x1FFFE8, 21), (0x7FFFF3, 23),
    (0x3FFFEA, 22), (0x3FFFEB, 22), (0x1FFFFEE, 25), (0x1FFFFEF, 25),
    (0xFFFFF4, 24), (0xFFFFF5, 24), (0x3FFFFEA, 26), (0x7FFFF4, 23),
    (0x3FFFFEB, 26), (0x7FFFFE6, 27), (0x3FFFFEC, 26), (0x3FFFFED, 26),
    (0x7FFFFE7, 27), (0x7FFFFE8, 27), (0x7FFFFE9, 27), (0x7FFFFEA, 27),
    (0x7FFFFEB, 27), (0xFFFFFFE, 28), (0x7FFFFEC, 27), (0x7FFFFED, 27),
    (0x7FFFFEE, 27), (0x7FFFFEF, 27), (0x7FFFFF0, 27), (0x3FFFFEE, 26),
    (0x3FFFFFFF, 30),
]


def _build_huffman_tree() -> dict[Any, Any]:
    root: dict[Any, Any] = {}
    for sym, (code, bits) in enumerate(_HUFFMAN_CODES):
        node = root
        for shift in range(bits - 1, -1, -1):
            bit = (code >> shift) & 1
            if bit not in node:
                node[bit] = {}
            node = node[bit]
        node["sym"] = sym
    return root


_HUFFMAN_TREE = _build_huffman_tree()


def hpack_huffman_decode(data: bytes) -> bytes:
    out = bytearray()
    node = _HUFFMAN_TREE
    depth = 0
    acc_ones = True
    for byte in data:
        for shift in range(7, -1, -1):
            bit = (byte >> shift) & 1
            if bit == 0:
                acc_ones = False
            node = node.get(bit)
            if node is None:
                raise ValueError("invalid HPACK Huffman sequence")
            depth += 1
            sym = node.get("sym")
            if sym is not None:
                if sym == 256:
                    raise ValueError("EOS symbol encountered in HPACK Huffman")
                out.append(sym)
                node = _HUFFMAN_TREE
                depth = 0
                acc_ones = True
    if depth > 7 or (depth > 0 and not acc_ones):
        raise ValueError("invalid HPACK Huffman padding")
    return bytes(out)


def hpack_huffman_encode(data: bytes) -> bytes:
    acc = 0
    nbits = 0
    out = bytearray()
    for b in data:
        code, bits = _HUFFMAN_CODES[b]
        acc = (acc << bits) | code
        nbits += bits
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1 if nbits else 0
    if nbits > 0:
        pad = (1 << (8 - nbits)) - 1
        out.append(((acc << (8 - nbits)) | pad) & 0xFF)
    return bytes(out)


def _hpack_encode_int(value: int, prefix_bits: int, prefix_mask: int = 0) -> bytes:
    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return bytes([prefix_mask | value])
    out = bytearray([prefix_mask | max_prefix])
    value -= max_prefix
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _hpack_decode_int(buf: bytes, pos: int, prefix_bits: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise ValueError("truncated HPACK integer")
    max_prefix = (1 << prefix_bits) - 1
    val = buf[pos] & max_prefix
    pos += 1
    if val < max_prefix:
        return val, pos
    m = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val += (b & 0x7F) << m
        m += 7
        if not (b & 0x80):
            return val, pos
    raise ValueError("truncated HPACK integer continuation")


def _hpack_encode_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _hpack_encode_int(len(raw), 7, 0x00) + raw


def _hpack_decode_str(buf: bytes, pos: int) -> tuple[str, int]:
    if pos >= len(buf):
        raise ValueError("truncated HPACK string")
    huff = bool(buf[pos] & 0x80)
    length, pos = _hpack_decode_int(buf, pos, 7)
    if pos + length > len(buf):
        raise ValueError("truncated HPACK string payload")
    raw = buf[pos : pos + length]
    pos += length
    if huff:
        raw = hpack_huffman_decode(raw)
    return raw.decode("utf-8", errors="replace"), pos


class _HpackDecoder:
    def __init__(self, max_table_size: int = 4096) -> None:
        self.max_table_size = max_table_size
        self.dynamic: list[tuple[str, str]] = []
        self.dynamic_size = 0

    def _lookup(self, idx: int) -> tuple[str, str]:
        if idx <= 0:
            raise ValueError(f"invalid HPACK index {idx}")
        if idx < len(_HPACK_STATIC):
            return _HPACK_STATIC[idx]
        d_idx = idx - len(_HPACK_STATIC)
        if d_idx >= len(self.dynamic):
            raise ValueError(f"HPACK dynamic index {idx} out of range")
        return self.dynamic[d_idx]

    def _evict(self) -> None:
        while self.dynamic and self.dynamic_size > self.max_table_size:
            name, val = self.dynamic.pop()
            self.dynamic_size -= len(name.encode("utf-8")) + len(val.encode("utf-8")) + 32

    def _add(self, name: str, val: str) -> None:
        entry_size = len(name.encode("utf-8")) + len(val.encode("utf-8")) + 32
        self.dynamic.insert(0, (name, val))
        self.dynamic_size += entry_size
        self._evict()

    def decode(self, block: bytes) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        pos = 0
        n = len(block)
        while pos < n:
            b = block[pos]
            if b & 0x80:
                idx, pos = _hpack_decode_int(block, pos, 7)
                headers.append(self._lookup(idx))
            elif (b & 0xC0) == 0x40:
                idx, pos = _hpack_decode_int(block, pos, 6)
                if idx == 0:
                    name, pos = _hpack_decode_str(block, pos)
                else:
                    name, _ = self._lookup(idx)
                val, pos = _hpack_decode_str(block, pos)
                self._add(name, val)
                headers.append((name, val))
            elif (b & 0xE0) == 0x20:
                new_size, pos = _hpack_decode_int(block, pos, 5)
                self.max_table_size = min(new_size, 65536)
                self._evict()
            else:
                # Literal without indexing (0000xxxx) or never indexed (0001xxxx)
                idx, pos = _hpack_decode_int(block, pos, 4)
                if idx == 0:
                    name, pos = _hpack_decode_str(block, pos)
                else:
                    name, _ = self._lookup(idx)
                val, pos = _hpack_decode_str(block, pos)
                headers.append((name, val))
        return headers


def _hpack_encode_request_headers(
    authority: str,
    path: str,
    extra_headers: Optional[list[tuple[str, str]]] = None,
) -> bytes:
    out = bytearray()
    out.append(0x83)  # :method POST (static index 3)
    out.append(0x86)  # :scheme http (static index 6)
    # :path (static index 4, literal without indexing)
    out.extend(b"\x04" + _hpack_encode_str(path))
    # :authority (static index 1)
    out.extend(b"\x01" + _hpack_encode_str(authority))
    # content-type (static index 31)
    out.extend(b"\x0f\x10" + _hpack_encode_str("application/grpc"))
    # te: trailers
    out.extend(b"\x00" + _hpack_encode_str("te") + _hpack_encode_str("trailers"))
    if extra_headers:
        for k, v in extra_headers:
            kl = k.lower()
            if kl == "authorization":
                # static index 23
                out.extend(b"\x0f\x08" + _hpack_encode_str(v))
            else:
                out.extend(b"\x00" + _hpack_encode_str(kl) + _hpack_encode_str(v))
    return bytes(out)


# ---------------------------------------------------------------------------
# Minimal HTTP/2 h2c unary gRPC client
# ---------------------------------------------------------------------------

_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_FRAME_DATA = 0x0
_FRAME_HEADERS = 0x1
_FRAME_RST_STREAM = 0x3
_FRAME_SETTINGS = 0x4
_FRAME_PING = 0x6
_FRAME_GOAWAY = 0x7
_FRAME_WINDOW_UPDATE = 0x8
_FRAME_CONTINUATION = 0x9

_FLAG_END_STREAM = 0x1
_FLAG_ACK = 0x1
_FLAG_END_HEADERS = 0x4
_FLAG_PADDED = 0x8
_FLAG_PRIORITY = 0x20


def _pack_frame(ftype: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    header = bytes([(length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF, ftype, flags])
    header += struct.pack("!I", stream_id & 0x7FFFFFFF)
    return header + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        part = sock.recv(n - len(chunks))
        if not part:
            raise ConnectionError("EOF while reading HTTP/2 frame")
        chunks.extend(part)
    return bytes(chunks)


def _read_frame(sock: socket.socket) -> tuple[int, int, int, bytes]:
    hdr = _recv_exact(sock, 9)
    length = (hdr[0] << 16) | (hdr[1] << 8) | hdr[2]
    ftype = hdr[3]
    flags = hdr[4]
    stream_id = struct.unpack("!I", hdr[5:9])[0] & 0x7FFFFFFF
    payload = _recv_exact(sock, length) if length else b""
    return ftype, flags, stream_id, payload


class GrpcError(RuntimeError):
    def __init__(self, status: int, message: str, transport_error: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.transport_error = transport_error


class _H2GrpcSession:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.sock: Optional[socket.socket] = None
        self.next_stream_id = 1
        self.hpack = _HpackDecoder()

    def __enter__(self) -> "_H2GrpcSession":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            self.sock.sendall(_H2_PREFACE + _pack_frame(_FRAME_SETTINGS, 0, 0, b""))
            # Read initial server SETTINGS (or handle PING/WINDOW_UPDATE)
            while True:
                ftype, flags, stream_id, payload = _read_frame(self.sock)
                if ftype == _FRAME_SETTINGS:
                    if not (flags & _FLAG_ACK):
                        self.sock.sendall(_pack_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
                    break
                elif ftype == _FRAME_PING and not (flags & _FLAG_ACK):
                    self.sock.sendall(_pack_frame(_FRAME_PING, _FLAG_ACK, 0, payload))
                elif ftype == _FRAME_WINDOW_UPDATE:
                    continue
                elif ftype == _FRAME_GOAWAY:
                    raise GrpcError(14, "server sent GOAWAY during handshake", transport_error=True)
        except GrpcError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise GrpcError(14, f"gRPC connect failed: {exc}", transport_error=True) from exc

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def unary(
        self,
        path: str,
        request_bytes: bytes,
        token: Optional[str] = None,
    ) -> tuple[bytes, dict[str, str]]:
        if self.sock is None:
            raise GrpcError(14, "gRPC socket not connected", transport_error=True)
        sid = self.next_stream_id
        self.next_stream_id += 2
        extra = [("authorization", token)] if token else None
        authority = f"{self.host}:{self.port}"
        hdr_block = _hpack_encode_request_headers(authority, path, extra)
        grpc_frame = struct.pack("!BI", 0, len(request_bytes)) + request_bytes
        try:
            self.sock.sendall(
                _pack_frame(_FRAME_HEADERS, _FLAG_END_HEADERS, sid, hdr_block)
                + _pack_frame(_FRAME_DATA, _FLAG_END_STREAM, sid, grpc_frame)
            )
        except OSError as exc:
            raise GrpcError(14, f"gRPC send failed: {exc}", transport_error=True) from exc

        resp_headers: dict[str, str] = {}
        resp_data = bytearray()
        pending_hdr_block = bytearray()
        reading_headers = False
        stream_ended = False

        while not stream_ended:
            try:
                ftype, flags, frame_sid, payload = _read_frame(self.sock)
            except Exception as exc:
                raise GrpcError(14, f"gRPC read failed: {exc}", transport_error=True) from exc

            if frame_sid == 0:
                if ftype == _FRAME_SETTINGS and not (flags & _FLAG_ACK):
                    self.sock.sendall(_pack_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
                elif ftype == _FRAME_PING and not (flags & _FLAG_ACK):
                    self.sock.sendall(_pack_frame(_FRAME_PING, _FLAG_ACK, 0, payload))
                elif ftype == _FRAME_GOAWAY:
                    if len(payload) >= 8:
                        err_code = struct.unpack("!I", payload[4:8])[0]
                        if err_code != 0 and not resp_data:
                            raise GrpcError(14, f"gRPC GOAWAY error {err_code}", transport_error=True)
                continue

            if frame_sid != sid:
                continue

            if ftype == _FRAME_RST_STREAM:
                err_code = struct.unpack("!I", payload[:4])[0] if len(payload) >= 4 else 0
                raise GrpcError(14, f"gRPC RST_STREAM {err_code}", transport_error=True)

            if ftype == _FRAME_HEADERS:
                pos = 0
                pad_len = 0
                if flags & _FLAG_PADDED:
                    pad_len = payload[0]
                    pos += 1
                if flags & _FLAG_PRIORITY:
                    pos += 5
                end = len(payload) - pad_len if pad_len else len(payload)
                pending_hdr_block.extend(payload[pos:end])
                reading_headers = True
                if flags & _FLAG_END_HEADERS:
                    for k, v in self.hpack.decode(bytes(pending_hdr_block)):
                        resp_headers[k.lower()] = v
                    pending_hdr_block.clear()
                    reading_headers = False
                if flags & _FLAG_END_STREAM:
                    stream_ended = True

            elif ftype == _FRAME_CONTINUATION and reading_headers:
                pending_hdr_block.extend(payload)
                if flags & _FLAG_END_HEADERS:
                    for k, v in self.hpack.decode(bytes(pending_hdr_block)):
                        resp_headers[k.lower()] = v
                    pending_hdr_block.clear()
                    reading_headers = False
                if flags & _FLAG_END_STREAM:
                    stream_ended = True

            elif ftype == _FRAME_DATA:
                pos = 0
                pad_len = 0
                if flags & _FLAG_PADDED:
                    pad_len = payload[0]
                    pos += 1
                end = len(payload) - pad_len if pad_len else len(payload)
                chunk = payload[pos:end]
                resp_data.extend(chunk)
                if len(payload) > 0:
                    inc = struct.pack("!I", len(payload) & 0x7FFFFFFF)
                    try:
                        self.sock.sendall(
                            _pack_frame(_FRAME_WINDOW_UPDATE, 0, 0, inc)
                            + _pack_frame(_FRAME_WINDOW_UPDATE, 0, sid, inc)
                        )
                    except OSError:
                        pass
                if flags & _FLAG_END_STREAM:
                    stream_ended = True

        grpc_status = int(resp_headers.get("grpc-status", "0") or "0")
        grpc_msg = resp_headers.get("grpc-message", "")
        if grpc_status != 0:
            raise GrpcError(grpc_status, grpc_msg or f"gRPC status {grpc_status}", transport_error=False)

        raw = bytes(resp_data)
        if not raw:
            return b"", resp_headers
        if len(raw) < 5:
            raise GrpcError(13, "truncated gRPC data frame", transport_error=False)
        compressed = raw[0]
        msg_len = struct.unpack("!I", raw[1:5])[0]
        if compressed != 0:
            raise GrpcError(12, "compressed gRPC response unsupported", transport_error=False)
        msg_bytes = raw[5 : 5 + msg_len]
        return msg_bytes, resp_headers


# ---------------------------------------------------------------------------
# Braiins OS+ enum/message decoders
# ---------------------------------------------------------------------------

_TUNER_STATE_NAMES = {
    0: "UNSPECIFIED",
    1: "DISABLED",
    2: "STABLE",
    3: "TUNING",
    4: "ERROR",
    5: "CONTINUOUS",
    6: "PREHEAT",
}

_TUNER_MODE_NAMES = {
    0: "UNSPECIFIED",
    1: "POWER_TARGET",
    2: "HASHRATE_TARGET",
}


def _decode_power_msg(raw: Optional[bytes]) -> Optional[int]:
    if not raw:
        return None
    f = parse_proto_fields(raw)
    if 1 in f:
        return _first_varint(f, 1, 0)
    return None


def _decode_login_resp(raw: bytes, headers: dict[str, str]) -> str:
    if raw:
        f = parse_proto_fields(raw)
        tok = _first_str(f, 1, "")
        if tok:
            return tok
    return headers.get("authorization", "")


def _decode_tuner_state_resp(raw: bytes) -> dict[str, Any]:
    f = parse_proto_fields(raw)
    state_code = _first_varint(f, 1, 0)
    state_name = _TUNER_STATE_NAMES.get(state_code, str(state_code))
    pt_raw = _first_bytes(f, 2)
    ht_raw = _first_bytes(f, 3)
    mode = "UNKNOWN"
    current_w: Optional[int] = None
    if pt_raw is not None:
        mode = "POWER_TARGET"
        pt_f = parse_proto_fields(pt_raw)
        current_w = _decode_power_msg(_first_bytes(pt_f, 2))
    elif ht_raw is not None:
        mode = "HASHRATE_TARGET"
    return {
        "tuner_state": state_name,
        "tuner_mode": mode,
        "current_power_target_w": current_w,
    }


def _decode_constraints_resp(raw: bytes) -> dict[str, Optional[int]]:
    f = parse_proto_fields(raw)
    tc_raw = _first_bytes(f, 1)
    if not tc_raw:
        return {"min_w": None, "max_w": None, "default_w": None}
    tc_f = parse_proto_fields(tc_raw)
    pt_raw = _first_bytes(tc_f, 1)
    if not pt_raw:
        return {"min_w": None, "max_w": None, "default_w": None}
    pt_f = parse_proto_fields(pt_raw)
    return {
        "min_w": _decode_power_msg(_first_bytes(pt_f, 1)),
        "max_w": _decode_power_msg(_first_bytes(pt_f, 2)),
        "default_w": _decode_power_msg(_first_bytes(pt_f, 3)),
    }


def _decode_miner_config_resp(raw: bytes) -> dict[str, Any]:
    f = parse_proto_fields(raw)
    tuner_raw = _first_bytes(f, 3)
    if not tuner_raw:
        return {"tuner_enabled": None, "tuner_mode": "UNKNOWN", "power_target_w": None}
    tf = parse_proto_fields(tuner_raw)
    enabled = bool(_first_varint(tf, 1, 0)) if 1 in tf else None
    mode_code = _first_varint(tf, 2, 0)
    mode_name = _TUNER_MODE_NAMES.get(mode_code, "UNKNOWN") if 2 in tf else "UNKNOWN"
    pw = _decode_power_msg(_first_bytes(tf, 3))
    return {
        "tuner_enabled": enabled,
        "tuner_mode": mode_name,
        "power_target_w": pw,
    }


# ---------------------------------------------------------------------------
# REST fallback helpers (Braiins OS 25.07+ /api/v1)
# ---------------------------------------------------------------------------


def _rest_request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    url = f"http://{host}:{int(port)}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}" if not token.startswith("Bearer ") else token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(raw) if raw else {"error": str(exc)}
        except json.JSONDecodeError:
            parsed = {"error": raw or str(exc)}
        return exc.code, parsed
    except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        raise ConnectionError(f"REST {method} {path} failed: {exc}") from exc


def _rest_login(host: str, port: int, username: str, password: str, timeout: float) -> str:
    code, data = _rest_request(
        host,
        port,
        "POST",
        "/api/v1/auth/login",
        {"username": username or "root", "password": password or ""},
        timeout=timeout,
    )
    if code == 401 or code == 403:
        raise PermissionError(f"REST login rejected (HTTP {code})")
    if code >= 400 or not isinstance(data, dict) or not data.get("token"):
        raise ConnectionError(f"REST login unavailable (HTTP {code})")
    return str(data["token"])


def _rest_test_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    timeout: float,
) -> dict[str, Any]:
    token = _rest_login(host, port, username, password, timeout)
    t_code, t_data = _rest_request(host, port, "GET", "/api/v1/performance/tuner-state", token=token, timeout=timeout)
    if t_code == 401:
        # Some builds accept raw token without Bearer prefix
        t_code, t_data = _rest_request(host, port, "GET", "/api/v1/performance/tuner-state", token=token, timeout=timeout)
    c_code, c_data = _rest_request(host, port, "GET", "/api/v1/configuration/constraints", token=token, timeout=timeout)
    m_code, m_data = _rest_request(host, port, "GET", "/api/v1/configuration/miner", token=token, timeout=timeout)

    tuner_state = "UNKNOWN"
    tuner_mode = "UNKNOWN"
    cur_w: Optional[int] = None
    if t_code == 200 and isinstance(t_data, dict):
        raw_state = t_data.get("overall_tuner_state")
        if isinstance(raw_state, int):
            tuner_state = _TUNER_STATE_NAMES.get(raw_state, str(raw_state))
        elif isinstance(raw_state, str) and raw_state:
            tuner_state = raw_state.upper()
        ms = t_data.get("mode_state") or {}
        if isinstance(ms, dict):
            pt = ms.get("power_target_mode_state")
            if isinstance(pt, dict):
                tuner_mode = "POWER_TARGET"
                ct = pt.get("current_target") or {}
                if isinstance(ct, dict) and ct.get("watt") is not None:
                    cur_w = int(ct["watt"])
            elif "hashrate_target_mode_state" in ms:
                tuner_mode = "HASHRATE_TARGET"

    min_w = max_w = def_w = None
    if c_code == 200 and isinstance(c_data, dict):
        tc = c_data.get("tuner_constraints") or {}
        ptc = tc.get("power_target") or {}
        if isinstance(ptc, dict):
            if isinstance(ptc.get("min"), dict) and ptc["min"].get("watt") is not None:
                min_w = int(ptc["min"]["watt"])
            if isinstance(ptc.get("max"), dict) and ptc["max"].get("watt") is not None:
                max_w = int(ptc["max"]["watt"])
            if isinstance(ptc.get("default"), dict) and ptc["default"].get("watt") is not None:
                def_w = int(ptc["default"]["watt"])

    tuner_enabled: Optional[bool] = None
    if m_code == 200 and isinstance(m_data, dict):
        tuner_cfg = m_data.get("tuner") or {}
        if isinstance(tuner_cfg, dict):
            if "enabled" in tuner_cfg:
                tuner_enabled = bool(tuner_cfg["enabled"])
            if tuner_mode == "UNKNOWN" and tuner_cfg.get("tuner_mode"):
                tm = tuner_cfg["tuner_mode"]
                tuner_mode = _TUNER_MODE_NAMES.get(tm, str(tm).upper()) if isinstance(tm, int) else str(tm).upper()
            if cur_w is None and isinstance(tuner_cfg.get("power_target"), dict):
                w = tuner_cfg["power_target"].get("watt")
                if w is not None:
                    cur_w = int(w)

    writes_supported = bool(
        t_code == 200
        and (tuner_enabled is not False)
        and (tuner_state != "DISABLED")
        and (tuner_mode in ("POWER_TARGET", "UNKNOWN"))
    )
    msg = (
        f"REST :{(port)} connected (tuner={tuner_state}, mode={tuner_mode}"
        + (f", current={cur_w}W" if cur_w is not None else "")
        + (f", range={min_w}-{max_w}W" if min_w is not None and max_w is not None else "")
        + ")"
    )
    return {
        "ok": True,
        "transport": "rest",
        "authenticated": True,
        "tuner_state": tuner_state,
        "tuner_mode": tuner_mode,
        "tuner_enabled": tuner_enabled,
        "current_power_target_w": cur_w,
        "constraints_min_w": min_w,
        "constraints_max_w": max_w,
        "constraints_default_w": def_w,
        "writes_supported": writes_supported,
        "message": msg,
    }


def _grpc_login(sess: _H2GrpcSession, username: str, password: str) -> str:
    req = encode_field_str(1, username or "root") + encode_field_str(2, password or "")
    raw, headers = sess.unary("/braiins.bos.v1.AuthenticationService/Login", req)
    token = _decode_login_resp(raw, headers)
    if not token:
        raise GrpcError(16, "empty auth token from Login", transport_error=False)
    return token


def _grpc_test_connection(
    host: str,
    grpc_port: int,
    username: str,
    password: str,
    timeout: float,
) -> dict[str, Any]:
    with _H2GrpcSession(host, grpc_port, timeout=timeout) as sess:
        token = _grpc_login(sess, username, password)
        ts_raw, _ = sess.unary("/braiins.bos.v1.PerformanceService/GetTunerState", b"", token=token)
        ts = _decode_tuner_state_resp(ts_raw)
        constraints = {"min_w": None, "max_w": None, "default_w": None}
        try:
            c_raw, _ = sess.unary("/braiins.bos.v1.ConfigurationService/GetConstraints", b"", token=token)
            constraints = _decode_constraints_resp(c_raw)
        except GrpcError:
            pass
        m_cfg = {"tuner_enabled": None, "tuner_mode": "UNKNOWN", "power_target_w": None}
        try:
            m_raw, _ = sess.unary("/braiins.bos.v1.ConfigurationService/GetMinerConfiguration", b"", token=token)
            m_cfg = _decode_miner_config_resp(m_raw)
        except GrpcError:
            pass

    tuner_state = ts["tuner_state"]
    tuner_mode = ts["tuner_mode"] if ts["tuner_mode"] != "UNKNOWN" else m_cfg["tuner_mode"]
    cur_w = ts["current_power_target_w"] if ts["current_power_target_w"] is not None else m_cfg["power_target_w"]
    tuner_enabled = m_cfg["tuner_enabled"]
    writes_supported = bool(
        (tuner_enabled is not False)
        and (tuner_state != "DISABLED")
        and (tuner_mode in ("POWER_TARGET", "UNKNOWN"))
    )
    msg = (
        f"gRPC :{grpc_port} connected (tuner={tuner_state}, mode={tuner_mode}"
        + (f", current={cur_w}W" if cur_w is not None else "")
        + (
            f", range={constraints['min_w']}-{constraints['max_w']}W"
            if constraints["min_w"] is not None and constraints["max_w"] is not None
            else ""
        )
        + ")"
    )
    return {
        "ok": True,
        "transport": "grpc",
        "authenticated": True,
        "tuner_state": tuner_state,
        "tuner_mode": tuner_mode,
        "tuner_enabled": tuner_enabled,
        "current_power_target_w": cur_w,
        "constraints_min_w": constraints["min_w"],
        "constraints_max_w": constraints["max_w"],
        "constraints_default_w": constraints["default_w"],
        "writes_supported": writes_supported,
        "message": msg,
    }


def test_connection(
    host: str,
    username: str = "root",
    password: str = "",
    grpc_port: int = 50051,
    http_port: int = 80,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Read-only check against Braiins OS+ Public API (gRPC :50051, REST :80 fallback)."""
    grpc_err: Optional[str] = None
    try:
        return _grpc_test_connection(host, grpc_port, username, password, timeout)
    except GrpcError as exc:
        grpc_err = f"gRPC status {exc.status}: {exc.message}"
        # If authentication explicitly failed on gRPC, still surface clearly unless REST works
        if exc.status in (16, 7):  # UNAUTHENTICATED / PERMISSION_DENIED
            return {
                "ok": False,
                "transport": "grpc",
                "authenticated": False,
                "tuner_state": "UNKNOWN",
                "tuner_mode": "UNKNOWN",
                "tuner_enabled": None,
                "current_power_target_w": None,
                "constraints_min_w": None,
                "constraints_max_w": None,
                "constraints_default_w": None,
                "writes_supported": False,
                "message": f"gRPC authentication failed ({exc.message}). Check username/password.",
            }
    except Exception as exc:
        grpc_err = str(exc)

    try:
        return _rest_test_connection(host, http_port, username, password, timeout)
    except PermissionError as exc:
        return {
            "ok": False,
            "transport": "rest",
            "authenticated": False,
            "tuner_state": "UNKNOWN",
            "tuner_mode": "UNKNOWN",
            "tuner_enabled": None,
            "current_power_target_w": None,
            "constraints_min_w": None,
            "constraints_max_w": None,
            "constraints_default_w": None,
            "writes_supported": False,
            "message": f"REST authentication failed: {exc}",
        }
    except Exception as rest_exc:
        return {
            "ok": False,
            "transport": "none",
            "authenticated": False,
            "tuner_state": "UNKNOWN",
            "tuner_mode": "UNKNOWN",
            "tuner_enabled": None,
            "current_power_target_w": None,
            "constraints_min_w": None,
            "constraints_max_w": None,
            "constraints_default_w": None,
            "writes_supported": False,
            "message": f"Braiins API unreachable (gRPC: {grpc_err}; REST: {rest_exc})",
        }


def set_power_target(
    host: str,
    watt: int,
    username: str = "root",
    password: str = "",
    grpc_port: int = 50051,
    http_port: int = 80,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Set Braiins OS+ power target (in Watts) via gRPC SetPowerTarget or REST fallback."""
    target_w = int(watt)
    grpc_err: Optional[str] = None
    try:
        with _H2GrpcSession(host, grpc_port, timeout=timeout) as sess:
            token = _grpc_login(sess, username, password)
            # SetPowerTargetRequest { save_action = 2 (SAVE_ACTION_SAVE_AND_APPLY), power_target = Power { watt } }
            power_msg = encode_field_varint(1, target_w)
            req = encode_field_varint(1, 2) + encode_field_bytes(2, power_msg)
            resp_raw, _ = sess.unary("/braiins.bos.v1.PerformanceService/SetPowerTarget", req, token=token)
            applied_w = target_w
            if resp_raw:
                rf = parse_proto_fields(resp_raw)
                parsed_w = _decode_power_msg(_first_bytes(rf, 1))
                if parsed_w is not None:
                    applied_w = parsed_w
            return {"ok": True, "transport": "grpc", "power_target_w": applied_w}
    except GrpcError as exc:
        grpc_err = f"gRPC status {exc.status}: {exc.message}"
        if exc.status in (16, 7) or not exc.transport_error:
            # Server responded with gRPC application error (e.g. invalid argument / unauthenticated)
            if exc.status != 12:  # UNIMPLEMENTED falls through to REST
                raise RuntimeError(grpc_err) from exc
    except Exception as exc:
        grpc_err = str(exc)

    # REST fallback
    try:
        token = _rest_login(host, http_port, username, password, timeout)
        code, data = _rest_request(
            host,
            http_port,
            "PUT",
            "/api/v1/performance/power-target",
            {"watt": target_w},
            token=token,
            timeout=timeout,
        )
        if code in (200, 204):
            applied_w = int(data.get("watt", target_w)) if isinstance(data, dict) else target_w
            return {"ok": True, "transport": "rest", "power_target_w": applied_w}
        raise RuntimeError(f"REST SetPowerTarget HTTP {code}: {data}")
    except Exception as rest_exc:
        raise RuntimeError(f"Braiins SetPowerTarget failed (gRPC: {grpc_err}; REST: {rest_exc})") from rest_exc


def restart_miner(
    host: str,
    username: str = "root",
    password: str = "",
    grpc_port: int = 50051,
    http_port: int = 80,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """v1.9: restart the mining process (bosminer) via ActionsService/Restart, REST fallback.

    This restarts mining only (not a full reboot of the control board).
    """
    grpc_err: Optional[str] = None
    try:
        with _H2GrpcSession(host, grpc_port, timeout=timeout) as sess:
            token = _grpc_login(sess, username, password)
            sess.unary("/braiins.bos.v1.ActionsService/Restart", b"", token=token)
            return {"ok": True, "transport": "grpc"}
    except GrpcError as exc:
        grpc_err = f"gRPC status {exc.status}: {exc.message}"
        if not exc.transport_error and exc.status != 12:
            raise RuntimeError(grpc_err) from exc
    except Exception as exc:
        grpc_err = str(exc)
    try:
        token = _rest_login(host, http_port, username, password, timeout)
        code, data = _rest_request(host, http_port, "PUT", "/api/v1/actions/restart", {}, token=token, timeout=timeout)
        if code in (200, 202, 204):
            return {"ok": True, "transport": "rest"}
        raise RuntimeError(f"REST restart HTTP {code}: {data}")
    except Exception as rest_exc:
        raise RuntimeError(f"Braiins restart failed (gRPC: {grpc_err}; REST: {rest_exc})") from rest_exc


# ---------------------------------------------------------------------------
# v1.10: fault visibility + resume (never factory reset)
# ---------------------------------------------------------------------------

def resume_miner(
    host: str,
    username: str = "root",
    password: str = "",
    grpc_port: int = 50051,
    http_port: int = 80,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Resume mining after a pause (e.g. thermal pause) via ActionsService/ResumeMining, REST fallback."""
    grpc_err: Optional[str] = None
    try:
        with _H2GrpcSession(host, grpc_port, timeout=timeout) as sess:
            token = _grpc_login(sess, username, password)
            sess.unary("/braiins.bos.v1.ActionsService/ResumeMining", b"", token=token)
            return {"ok": True, "transport": "grpc"}
    except GrpcError as exc:
        grpc_err = f"gRPC status {exc.status}: {exc.message}"
        if not exc.transport_error and exc.status != 12:
            raise RuntimeError(grpc_err) from exc
    except Exception as exc:
        grpc_err = str(exc)
    try:
        token = _rest_login(host, http_port, username, password, timeout)
        code, data = _rest_request(host, http_port, "PUT", "/api/v1/actions/resume", {}, token=token, timeout=timeout)
        if code in (200, 202, 204):
            return {"ok": True, "transport": "rest"}
        raise RuntimeError(f"REST resume HTTP {code}: {data}")
    except Exception as rest_exc:
        raise RuntimeError(f"Braiins resume failed (gRPC: {grpc_err}; REST: {rest_exc})") from rest_exc


def _walk_errors(obj: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, list):
        for x in obj:
            _walk_errors(x, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    codes = obj.get("error_codes") or obj.get("errorCodes")
    msg = str(obj.get("message") or obj.get("reason") or obj.get("name") or "").strip()
    if isinstance(codes, list) and codes:
        for c in codes:
            if isinstance(c, dict):
                out.append({"code": str(c.get("code") or "").strip(), "message": str(c.get("name") or c.get("reason") or msg).strip(),
                            "hint": str(c.get("hint") or "").strip()})
            elif c:
                out.append({"code": str(c).strip(), "message": msg})
        return
    if "code" in obj and isinstance(obj.get("code"), (str, int)) and str(obj.get("code")).strip():
        out.append({"code": str(obj["code"]).strip(), "message": msg})
        return
    for key in ("errors", "items", "miner_errors"):
        if key in obj:
            _walk_errors(obj[key], out, depth + 1)
            return
    if msg:
        out.append({"code": "", "message": msg})


def parse_errors_payload(data: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _walk_errors(data, out)
    seen, uniq = set(), []
    for e in out:
        k = (e.get("code"), e.get("message"))
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq[:50]


_MINER_STATUS_NAMES = {
    0: "MINER_STATUS_UNSPECIFIED",
    1: "MINER_STATUS_NOT_STARTED",
    2: "MINER_STATUS_NORMAL",
    3: "MINER_STATUS_PAUSED",
    4: "MINER_STATUS_SUSPENDED",
    5: "MINER_STATUS_RESTRICTED",
}
_STOP_REASON_FIELDS = {
    1: "unspecified",
    2: "application_unavailable",
    3: "unsupported_hardware",
    4: "dead_pools",
    5: "missing_license",
    6: "user_pause",
    7: "thermal_pause",
    8: "dps_cooldown",
    9: "tuner_error",
    10: "hardware_error",
}
_START_REASON_FIELDS = {
    1: "none",
    2: "delayed_start",
    3: "cooling_down",
    4: "waiting_while_cold",
    5: "defrosting",
}
_THERMAL_PAUSE_REASONS = {
    0: "THERMAL_PAUSE_REASON_UNSPECIFIED",
    1: "THERMAL_PAUSE_REASON_DANGEROUS_TEMPERATURE",
    2: "THERMAL_PAUSE_REASON_BROKEN_FANS",
}


def _unwrap_status_dict(v: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if not isinstance(v, dict):
        return str(v).strip() if v not in (None, "") else ""
    for kk in ("status", "name", "state"):
        if v.get(kk):
            inner = v[kk]
            if isinstance(inner, dict):
                sub = _unwrap_status_dict(inner, depth + 1)
                return f"{kk}:{sub}" if sub else str(kk)
            return str(inner)
    # Protobuf oneof JSON (e.g. {"stopped": {"thermal_pause": {"reason": "..."}}})
    for arm in ("stopped", "stopping", "starting", "running"):
        if arm in v and v[arm] is not None:
            sub_val = v[arm]
            if isinstance(sub_val, dict) and sub_val:
                sub_k = next(iter(sub_val))
                inner = sub_val[sub_k]
                detail = ""
                if isinstance(inner, dict):
                    if inner.get("reason"):
                        detail = str(inner["reason"])
                    elif inner.get("message"):
                        detail = str(inner["message"])
                    elif isinstance(inner.get("errors"), list) and inner["errors"]:
                        detail = "; ".join(str(x) for x in inner["errors"][:3])
                return f"{arm}:{sub_k}" + (f" ({detail})" if detail else "")
            return arm
    if len(v) == 1:
        k = next(iter(v))
        sub = _unwrap_status_dict(v[k], depth + 1)
        return f"{k}:{sub}" if sub else str(k)
    return ""


def _status_text(details: Any) -> str:
    """Best-effort miner status string from /api/v1/miner/details (supports flat strings and nested protobuf-JSON oneofs)."""
    if not isinstance(details, dict):
        return ""
    for key in ("detailed_status", "detailedStatus", "status", "miner_status"):
        v = details.get(key)
        if v not in (None, ""):
            txt = _unwrap_status_dict(v)
            if txt:
                return txt
    return ""


def _decode_errors_resp(raw: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    f = parse_proto_fields(raw)
    for wt, err_raw in f.get(1, []):
        if wt != 2 or not isinstance(err_raw, bytes):
            continue
        ef = parse_proto_fields(err_raw)
        msg = _first_str(ef, 2)
        codes_raw = [x for wt, x in ef.get(3, []) if wt == 2 and isinstance(x, bytes)]
        if codes_raw:
            for c_raw in codes_raw:
                cf = parse_proto_fields(c_raw)
                code = _first_str(cf, 1)
                reason = _first_str(cf, 2) or msg
                hint = _first_str(cf, 3)
                out.append({"code": code, "message": reason, "hint": hint})
        elif msg:
            out.append({"code": "", "message": msg})
    return out[:50]


def _decode_miner_details_resp(raw: bytes) -> str:
    f = parse_proto_fields(raw)
    ds_raw = _first_bytes(f, 18)
    if ds_raw:
        df = parse_proto_fields(ds_raw)
        for field_no, arm in ((1, "stopped"), (4, "stopping")):
            sr_raw = _first_bytes(df, field_no)
            if sr_raw is not None:
                sf = parse_proto_fields(sr_raw)
                for rno, rname in _STOP_REASON_FIELDS.items():
                    if rno in sf:
                        detail = ""
                        inner_raw = _first_bytes(sf, rno)
                        if inner_raw:
                            inf = parse_proto_fields(inner_raw)
                            if rno == 7 and 1 in inf:
                                detail = _THERMAL_PAUSE_REASONS.get(_first_varint(inf, 1, 0), "")
                            elif rno in (9, 10):
                                detail = _first_str(inf, 1)
                            elif rno == 3:
                                errs = [x.decode("utf-8", errors="replace") for wt, x in inf.get(1, []) if wt == 2 and isinstance(x, bytes)]
                                detail = "; ".join(errs[:3])
                        return f"{arm}:{rname}" + (f" ({detail})" if detail else "")
                return arm
        st_raw = _first_bytes(df, 2)
        if st_raw is not None:
            stf = parse_proto_fields(st_raw)
            for rno, rname in _START_REASON_FIELDS.items():
                if rno in stf:
                    return f"starting:{rname}"
            return "starting"
        if 3 in df:
            return "running"
    if 12 in f:
        return _MINER_STATUS_NAMES.get(_first_varint(f, 12, 0), "")
    return ""


def get_errors(
    host: str,
    username: str,
    password: str,
    http_port: int = 80,
    timeout: float = 5.0,
    grpc_port: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch Braiins OS+ errors & detailed_status (REST primary, gRPC fallback when grpc_port > 0)."""
    rest_err: Optional[Exception] = None
    try:
        token = _rest_login(host, http_port, username, password, timeout)
        code, data = _rest_request(host, http_port, "GET", "/api/v1/miner/errors", None, token=token, timeout=timeout)
        errors = parse_errors_payload(data) if code == 200 else []
        status = ""
        try:
            c2, det = _rest_request(host, http_port, "GET", "/api/v1/miner/details", None, token=token, timeout=timeout)
            if c2 == 200:
                status = _status_text(det)
        except ConnectionError:
            pass
        if code not in (200, 404):
            raise RuntimeError(f"Braiins errors HTTP {code}")
        return errors, status
    except PermissionError:
        raise
    except ConnectionError as exc:
        rest_err = exc
    if grpc_port and grpc_port > 0:
        try:
            with _H2GrpcSession(host, int(grpc_port), timeout=timeout) as sess:
                token = _grpc_login(sess, username, password)
                eraw, _ = sess.unary("/braiins.bos.v1.MinerService/GetErrors", b"", token=token)
                errors = _decode_errors_resp(eraw)
                status = ""
                try:
                    draw, _ = sess.unary("/braiins.bos.v1.MinerService/GetMinerDetails", b"", token=token)
                    status = _decode_miner_details_resp(draw)
                except GrpcError:
                    pass
                return errors, status
        except GrpcError as gexc:
            if gexc.status in (16, 7):
                raise PermissionError(f"gRPC authentication failed ({gexc.message})") from gexc
            raise RuntimeError(f"Braiins gRPC errors failed: {gexc}") from gexc
    assert rest_err is not None
    raise rest_err


def get_log(host: str, username: str, password: str, http_port: int = 80, which: str = "errors", timeout: float = 8.0) -> str:
    """REST GET /api/v1/miner/log/{errors|bosminer|syslog} as text."""
    if which not in {"errors", "bosminer", "boser", "monitor", "syslog", "dmesg"}:
        raise ValueError("unknown log")
    token = _rest_login(host, http_port, username, password, timeout)
    url = f"http://{host}:{int(http_port)}/api/v1/miner/log/{which}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "text/plain, application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Braiins log HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ConnectionError(f"Braiins log failed: {exc}") from exc
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                for k in ("log", "lines", "content", "data"):
                    if k in d:
                        d = d[k]
                        break
            if isinstance(d, list):
                return "\n".join(str(x.get("message", x) if isinstance(x, dict) else x) for x in d)
            return str(d)
        except json.JSONDecodeError:
            pass
    return raw
