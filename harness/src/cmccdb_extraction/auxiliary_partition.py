"""Lossless, immutable transport for large JSON audit attachments.

Public entry points return Paths with the manifest last. Every returned file is
valid JSON.gz and is at most max_bytes (5 MiB by default). Part envelopes carry
base64 fragments of one whole compressed gzip stream, not scientific data
substitutions. Compressing JSON once avoids expanding repeated audit content
into separately compressed raw-JSON base64 envelopes. The entire ORIGINAL
JSON byte stream, including whitespace, key order, duplicate keys and number
spelling, is reconstructed and SHA256 verified. No JSON reserialization,
truncation, original-file edits, exporter calls or database changes occur.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import uuid
import zlib
from pathlib import Path
from typing import Any

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MANIFEST_FORMAT = 'cmccdb-lossless-json-partition-v2'
FRAGMENT_FORMAT = 'cmccdb-gzip-byte-fragment-v2'
LEGACY_MANIFEST_FORMAT = 'cmccdb-lossless-json-partition-v1'
LEGACY_FRAGMENT_FORMAT = 'cmccdb-json-byte-fragment-v1'
MAX_BODY_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def _gunzip(encoded: bytes, limit: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as handle:
            raw = handle.read(limit + 1)
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError('Invalid gzip attachment') from error
    if len(raw) > limit:
        raise ValueError('Attachment exceeds bounded decompression limit')
    return raw


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _reject_constant(value: str) -> None:
    raise ValueError('Nonstandard JSON constant: ' + value)


def _validate_json(body: bytes) -> None:
    # Numbers are checked lexically without loss of precision, overflow to
    # infinity, or Python's huge-integer conversion limit. Original bytes stay
    # untouched. The parsed value is immediately discarded.
    json.loads(body, parse_int=str, parse_float=str,
               parse_constant=_reject_constant)


def _gzip(body: bytes) -> bytes:
    return gzip.compress(body, compresslevel=6, mtime=0)


def _write_immutable(path: Path, body: bytes) -> None:
    """Atomic no-overwrite publication; identical repeated calls are safe."""
    if path.exists():
        if path.read_bytes() != body:
            raise FileExistsError('Refusing to change existing attachment: ' + str(path))
        return
    tmp = path.with_name('.' + path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with tmp.open('xb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)  # Atomic and cannot replace an existing target.
        except FileExistsError:
            if path.read_bytes() != body:
                raise FileExistsError('Attachment collision: ' + str(path))
    finally:
        tmp.unlink(missing_ok=True)


def _partition(body: bytes, basename: str, output_dir: str | Path,
               max_bytes: int, source: dict | None = None, stream: bytes | None = None) -> list[Path]:
    if not isinstance(body, bytes):
        raise TypeError('body must be bytes, preserving the original JSON stream')
    if type(max_bytes) is not int or not 512 <= max_bytes <= MAX_ATTACHMENT_BYTES:
        raise ValueError('max_bytes must be an integer between 512 bytes and 5 MiB')
    _validate_json(body)
    name = Path(str(basename)).name
    stem = name
    while stem.endswith(('.json', '.gz')):
        stem = stem.rsplit('.', 1)[0]
    stem = re.sub('[^A-Za-z0-9._-]', '_', stem).strip('._-')[:64] or 'attachment'
    whole_sha = _sha(body)
    stream = _gzip(body) if stream is None else stream
    stream_sha = _sha(stream)
    # Include representation version, compressed content and limit: equal JSON
    # encoded differently or partitioned at another size never overwrites a file.
    prefix = stem + '.v2.' + whole_sha[:16] + '.' + stream_sha[:16] + '.' + str(max_bytes)
    prepared: list[tuple[int, bytes, bytes, str]] = []
    if len(stream) <= max_bytes:
        prepared.append((0, stream, stream, 'whole-gzip-stream'))
    else:
        chunk = max(1, (max_bytes - 512) * 3 // 4)
        pending = [(lo, min(lo + chunk, len(stream))) for lo in range(0, len(stream), chunk)]
        while pending:
            lo, hi = pending.pop()
            fragment = stream[lo:hi]
            envelope = dict(format=FRAGMENT_FORMAT, offset=lo,
                            fragment_bytes=len(fragment), fragment_sha256=_sha(fragment),
                            encoding='base64', payload=base64.b64encode(fragment).decode('ascii'))
            encoded = _gzip(_json_bytes(envelope))
            if len(encoded) > max_bytes:
                if hi - lo < 2:
                    raise ValueError('File size limit cannot hold the fragment envelope')
                middle = (lo + hi) // 2
                assert lo < middle < hi
                pending.extend([(middle, hi), (lo, middle)])
            else:
                prepared.append((lo, fragment, encoded, 'base64-gzip-fragment'))
    prepared.sort(key=lambda row: row[0])
    rows, files = [], []
    for number, (offset, fragment, encoded, encoding) in enumerate(prepared, 1):
        filename = (prefix + '.json.gz' if len(prepared) == 1 else
                    prefix + f'.part-{number:05d}.json.gz')
        assert len(encoded) <= max_bytes
        rows.append(dict(filename=filename, offset=offset,
                         fragment_bytes=len(fragment), fragment_sha256=_sha(fragment), encoding=encoding,
                         file_bytes=len(encoded), file_sha256=_sha(encoded)))
        files.append((filename, encoded))
    manifest = dict(format=MANIFEST_FORMAT, original_filename=name,
                    original_body_sha256=whole_sha, original_body_bytes=len(body),
                    compressed_stream_sha256=stream_sha, compressed_stream_bytes=len(stream),
                    stream_compression='gzip',
                    max_file_bytes=max_bytes, part_count=len(rows), parts=rows,
                    reassembly='Order by offset, decode gzip-stream fragments, concatenate exact compressed bytes and verify SHA256. Decompress boundedly, verify original JSON byte count/SHA256, then validate JSON.')
    if source:
        manifest['source_file'] = source
    manifest_name = prefix + '.manifest.json.gz'
    manifest_gzip = _gzip(_json_bytes(manifest))
    if len(manifest_gzip) > max_bytes:
        # Fail closed instead of truncating metadata or writing a manifest that
        # cannot be uploaded. No attachments have been published at this point.
        raise ValueError('Complete manifest exceeds file limit; increase transport limit or use a higher-level manifest index')
    files.append((manifest_name, manifest_gzip))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename, encoded in files:
        target = output / filename
        _write_immutable(target, encoded)
        paths.append(target)
    # Every published batch is verified immediately; no unverified successful
    # return can reach a caller's exporter attachment allowlist.
    assert reassemble_json_parts(paths[-1], expected_sha256=whole_sha,
                                 max_uncompressed_bytes=max(MAX_BODY_BYTES, len(body))) == body
    return paths


def partition_json_bytes(body: bytes, basename: str, output_dir: str | Path,
                         *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> list[Path]:
    """Partition an uncompressed JSON byte stream; manifest is the last Path."""
    return _partition(body, basename, output_dir, max_bytes)


def partition_audit(input_path: str | Path, output_dir: str | Path,
                    *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> list[Path]:
    """Read JSON.gz audit immutably, partition its exact decompressed body."""
    source_path = Path(input_path)
    encoded = source_path.read_bytes()
    body = _gunzip(encoded, MAX_BODY_BYTES)
    return _partition(body, source_path.name, output_dir, max_bytes,
                      source=dict(filename=source_path.name, bytes=len(encoded),
                                  sha256=_sha(encoded), compression='gzip'), stream=encoded)


def _reassemble(manifest_path: str | Path,
                          *, expected_sha256: str | None = None,
                          max_uncompressed_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Reassemble and verify hashes, lengths, contiguous offsets and JSON."""
    manifest_path = Path(manifest_path)
    manifest_encoded = manifest_path.read_bytes()
    if len(manifest_encoded) > MAX_ATTACHMENT_BYTES:
        raise ValueError('Manifest exceeds attachment file limit')
    manifest = json.loads(_gunzip(manifest_encoded, MAX_MANIFEST_BYTES))
    version = manifest.get('format')
    if version not in (MANIFEST_FORMAT, LEGACY_MANIFEST_FORMAT):
        raise ValueError('Unsupported partition manifest')
    limit = manifest['max_file_bytes']
    size = manifest['original_body_bytes']
    if (type(limit) is not int or not 512 <= limit <= MAX_ATTACHMENT_BYTES or
            type(size) is not int or not 0 <= size <= max_uncompressed_bytes):
        raise ValueError('Invalid/excessive manifest size limit')
    if len(manifest_encoded) > limit:
        raise ValueError('Manifest exceeds recorded file limit')
    if manifest['part_count'] != len(manifest['parts']):
        raise ValueError('Manifest part count mismatch')
    v2 = version == MANIFEST_FORMAT
    stream_size = manifest.get('compressed_stream_bytes') if v2 else size
    if (type(stream_size) is not int or not 0 <= stream_size <= max_uncompressed_bytes + MAX_ATTACHMENT_BYTES or
            (v2 and manifest.get('stream_compression') != 'gzip')):
        raise ValueError('Invalid/excessive compressed stream size')
    length_key = 'fragment_bytes' if v2 else 'body_fragment_bytes'
    sha_key = 'fragment_sha256' if v2 else 'body_fragment_sha256'
    chunks, offset, used = [], 0, set()
    for row in manifest['parts']:
        name = row['filename']
        length = row[length_key]
        if type(length) is not int or not 0 <= length <= stream_size - offset:
            raise ValueError('Invalid fragment length')
        if (not isinstance(name, str) or not name or Path(name).name != name or
                name.startswith('.') or '\\' in name or name in used):
            raise ValueError('Invalid/duplicate part filename')
        used.add(name)
        path = manifest_path.parent / name
        if path.resolve().parent != manifest_path.parent.resolve():
            raise ValueError('Part path escapes attachment directory')
        encoded = path.read_bytes()
        if (len(encoded) > limit or len(encoded) != row['file_bytes'] or
                _sha(encoded) != row['file_sha256']):
            raise ValueError('Compressed part size/hash mismatch: ' + name)
        if v2 and row['encoding'] == 'whole-gzip-stream':
            fragment = encoded
        elif not v2 and row['encoding'] == 'raw-json-body':
            raw = _gunzip(encoded, length)
            fragment = raw
            _validate_json(raw)
        elif row['encoding'] == ('base64-gzip-fragment' if v2 else 'base64-envelope'):
            raw = _gunzip(encoded, 4 * ((length + 2) // 3) + 4096)
            envelope = json.loads(raw)
            if (envelope.get('format') != (FRAGMENT_FORMAT if v2 else LEGACY_FRAGMENT_FORMAT) or
                    envelope.get('encoding') != 'base64' or
                    envelope.get('offset') != row['offset'] or
                    envelope.get(length_key) != row[length_key] or
                    envelope.get(sha_key) != row[sha_key]):
                raise ValueError('Fragment envelope/manifest mismatch')
            fragment = base64.b64decode(envelope['payload'], validate=True)
        else:
            raise ValueError('Unknown fragment encoding')
        if (row['offset'] != offset or len(fragment) != row[length_key] or
                _sha(fragment) != row[sha_key]):
            raise ValueError('Fragment offset/size/hash mismatch')
        offset += len(fragment)
        chunks.append(fragment)
    joined = b''.join(chunks)
    if v2:
        if len(joined) != stream_size or _sha(joined) != manifest['compressed_stream_sha256']:
            raise ValueError('Reassembled compressed stream size/hash mismatch')
        body = _gunzip(joined, size)
    else:
        body = joined
    if (len(body) != manifest['original_body_bytes'] or
            _sha(body) != manifest['original_body_sha256'] or
            (expected_sha256 and _sha(body) != expected_sha256)):
        raise ValueError('Reassembled original body size/hash mismatch')
    _validate_json(body)
    return body, joined if v2 else None


def reassemble_json_parts(manifest_path: str | Path, *, expected_sha256: str | None = None,
                          max_uncompressed_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Decode v1/v2 losslessly, checking every file, fragment, stream and JSON hash."""
    return _reassemble(manifest_path, expected_sha256=expected_sha256,
                       max_uncompressed_bytes=max_uncompressed_bytes)[0]


def reassemble_gzip_parts(manifest_path: str | Path, *, expected_sha256: str | None = None,
                          max_uncompressed_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Return the exact v2 whole gzip stream, after validating its JSON payload."""
    _, stream = _reassemble(manifest_path, expected_sha256=expected_sha256,
                            max_uncompressed_bytes=max_uncompressed_bytes)
    if stream is None:
        raise ValueError('Legacy v1 carries JSON bytes, not the original compressed stream')
    return stream


def verify_reassembly(manifest_path: str | Path, original_body: bytes | None = None) -> dict:
    body = reassemble_json_parts(manifest_path)
    if original_body is not None and body != original_body:
        raise ValueError('Reassembly differs from original byte stream')
    return dict(valid=True, original_body_bytes=len(body), original_body_sha256=_sha(body))


reassemble_audit = reassemble_json_parts
