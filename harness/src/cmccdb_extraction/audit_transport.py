"""Snapshot complete audit bytes and expose only bounded transport attachments."""

import gzip
import hashlib
import json
from pathlib import Path

from .auxiliary_partition import MAX_ATTACHMENT_BYTES, partition_audit, reassemble_json_parts, _write_immutable


LOGICAL_AUDIT_FILENAME = 'extraction-audit.json.gz'


def prepare_audit_transport(directory, body, *, max_bytes=MAX_ATTACHMENT_BYTES, snapshot=None):
    """Keep the archival JSON/gzip even when only its lossless parts fit uploads."""
    directory = Path(directory)
    body_sha = hashlib.sha256(body).hexdigest()
    if snapshot is not None:
        if snapshot['json_sha256'] != body_sha:
            raise ValueError('Audit transport snapshot does not match the complete audit')
        encoded = snapshot['archival_gzip']
        if hashlib.sha256(encoded).hexdigest() != snapshot['gzip_sha256']:
            raise ValueError('Audit transport archival snapshot hash mismatch')
    else:
        encoded = gzip.compress(body, mtime=0)
    (directory / 'extraction-audit.json').write_bytes(body)
    source = directory / LOGICAL_AUDIT_FILENAME
    source.write_bytes(encoded)
    if snapshot is not None:
        if set(snapshot['files']) != set(snapshot['blobs']):
            raise ValueError('Incomplete audit transport snapshot')
        for name, blob in snapshot['blobs'].items():
            if (not name or Path(name).name != name or '\\' in name or name.startswith('.') or
                    len(blob) > max_bytes or hashlib.sha256(blob).hexdigest() != snapshot['file_sha256'][name]):
                raise ValueError('Invalid audit transport snapshot file')
            _write_immutable(directory/name, blob)
        files = [directory/name for name in snapshot['files']]
        manifest = snapshot['manifest']
        reconstructed = (reassemble_json_parts(directory/manifest['filename']) if manifest else gzip.decompress(encoded))
        if reconstructed != body:
            raise ValueError('Audit transport snapshot reconstruction mismatch')
    elif len(encoded) <= max_bytes:
        files = [source]
        manifest = None
    else:
        files = partition_audit(source, directory, max_bytes=max_bytes)
        manifest = json.loads(gzip.decompress(files[-1].read_bytes()))
        manifest = dict(manifest, filename=files[-1].name,
                        logical_attachment_filename=LOGICAL_AUDIT_FILENAME)
    blobs = {path.name: path.read_bytes() for path in files}
    return dict(files=list(blobs), blobs=blobs, manifest=manifest,
                archival_gzip=encoded,
                file_sha256={name:hashlib.sha256(blob).hexdigest() for name,blob in blobs.items()},
                json_sha256=body_sha,
                gzip_sha256=hashlib.sha256(encoded).hexdigest())


def audit_transport_note(transport):
    manifest = transport.get('manifest')
    if manifest is None:
        return None
    return (f"Complete audit transport: {manifest['part_count']} gzip-stream fragment envelopes plus "
            f"{manifest['filename']}. Keep every file listed in audit_transport_files in the export receipt. "
            "Use cmccdb_extraction.auxiliary_partition.reassemble_gzip_parts(manifest_path) to reconstruct "
            f"the exact {LOGICAL_AUDIT_FILENAME} bytes for the logical provenance attachment, or "
            "reassemble_json_parts(manifest_path) for the exact extraction-audit.json bytes. "
            "Both helpers verify compressed-stream and original JSON hashes. "
            f"JSON SHA256: {transport['json_sha256']}. The original compressed audit remains archived; "
            "fragment envelopes are transport metadata and do not replace scientific evidence.")


def audit_documentation_rows(transport):
    """Only append read-only rows: cached ReactionData never needs rewriting."""
    note = audit_transport_note(transport)
    if note is None:
        return {}
    return {name: ['#!', 'Audit transport', '', note]
            for name in ('Overview', 'Review', 'Schema')}


def exported_resource_path(directory, filename):
    """Completed receipt allowlist and confinement apply to every export resource."""
    directory = Path(directory)
    if (not isinstance(filename, str) or not filename or
            Path(filename).name != filename or filename.startswith('.') or
            '\\' in filename):
        raise ValueError('Unknown export resource')
    receipt_path = directory / 'receipt.json'
    if (not receipt_path.is_file() or receipt_path.resolve().parent != directory.resolve()):
        raise ValueError('Export revision is incomplete')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    legacy = {'extraction.xlsx', 'extraction-audit.json', LOGICAL_AUDIT_FILENAME,
              'dataset.json', 'receipt.json'}
    if filename not in legacy | set(receipt.get('auxiliary_files', [])):
        raise ValueError('Unknown export resource')
    path = directory / filename
    if not path.is_file() or path.resolve().parent != directory.resolve():
        raise ValueError('Export resource escapes completed revision')
    return path
