"""Optional lossless response envelopes for large read-only MCP inspections."""
import base64
import binascii
import gzip
import hashlib
import io
import json
import zlib

from .schema import canonical

FORMAT = "cmccdb-gzip-json-v1"
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024


def inspection_response(payload, compressed=False):
    """Preserve the default payload; opt-in gzip never truncates source fields."""
    if not compressed:
        return payload
    raw = canonical(payload).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise ValueError("Compressed inspection response exceeds the 16 MiB gzip limit; no data was truncated")
    return dict(format=FORMAT, gzip_base64=base64.b64encode(blob).decode("ascii"),
                uncompressed_bytes=len(raw), json_sha256=hashlib.sha256(raw).hexdigest(),
                gzip_sha256=hashlib.sha256(blob).hexdigest())


def decode_inspection_response(envelope, max_uncompressed_bytes=512 * 1024 * 1024):
    """Verify both digests and exact length before returning decoded JSON.

    This local host helper is not an MCP tool. Its bounded read prevents a corrupt
    length from causing unbounded decompression; callers may set a larger limit.
    """
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT:
        raise ValueError("Unknown compressed inspection response format")
    expected = envelope.get("uncompressed_bytes")
    if type(expected) is not int or not 0 <= expected <= max_uncompressed_bytes:
        raise ValueError("Compressed inspection response has an invalid or excessive uncompressed length")
    encoded = envelope.get("gzip_base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_COMPRESSED_BYTES + 2) // 3):
        raise ValueError("Compressed inspection response exceeds the 16 MiB gzip limit")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Compressed inspection response has invalid base64") from error
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise ValueError("Compressed inspection response exceeds the 16 MiB gzip limit")
    if hashlib.sha256(blob).hexdigest() != envelope.get("gzip_sha256"):
        raise ValueError("Compressed inspection response gzip SHA256 mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
            raw = stream.read(expected + 1)
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError("Compressed inspection response has invalid gzip data") from error
    if len(raw) != expected:
        raise ValueError("Compressed inspection response uncompressed length mismatch")
    if hashlib.sha256(raw).hexdigest() != envelope.get("json_sha256"):
        raise ValueError("Compressed inspection response JSON SHA256 mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Compressed inspection response is not UTF-8 JSON") from error
    if not isinstance(payload, dict) or canonical(payload).encode("utf-8") != raw:
        raise ValueError("Compressed inspection response is not a canonical JSON object")
    return payload
