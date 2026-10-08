import base64
import gzip
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from cmccdb_extraction.auxiliary_partition import (partition_audit, partition_json_bytes,
                                 reassemble_json_parts, reassemble_gzip_parts, verify_reassembly)


class AttachmentPartitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='partition-tests-', dir=Path(__file__).parent)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def noise(n):
        return base64.b64encode(random.Random(n).randbytes(n)).decode('ascii')

    def assert_batch(self, body, paths, limit):
        self.assertGreater(len(paths), 1)
        for path in paths:
            self.assertLessEqual(path.stat().st_size, limit)
            json.loads(gzip.decompress(path.read_bytes()))
        self.assertEqual(reassemble_json_parts(paths[-1]), body)
        self.assertTrue(verify_reassembly(paths[-1], body)['valid'])

    def test_below_limit_and_original_compressed_sha(self):
        body = b'{  "same":1, "same":2, "number":1.00e+07, "items": [null, false] }\n'
        source = self.root / 'structure-revision-audit.json.gz'
        source.write_bytes(gzip.compress(body, mtime=123))
        original = source.read_bytes()
        paths = partition_audit(source, self.root / 'out', max_bytes=8192)
        self.assertEqual(len(paths), 2)
        self.assert_batch(body, paths, 8192)
        manifest = json.loads(gzip.decompress(paths[-1].read_bytes()))
        self.assertEqual(manifest['source_file']['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(reassemble_gzip_parts(paths[-1]), original)
        self.assertEqual(partition_audit(source, self.root / 'out', max_bytes=8192), paths)

    def test_recursive_array_records_and_nested_objects(self):
        obj = {'original_audit': {'records': [{'key': i, 'claims': [{'data': self.noise(1800+i)}]} for i in range(45)]},
               'structure_proposals': [{'review': self.noise(10000)}]}
        body = json.dumps(obj, indent=1).encode()
        paths = partition_json_bytes(body, 'audit.json', self.root / 'out', max_bytes=16384)
        self.assertGreater(len(paths), 3)
        self.assert_batch(body, paths, 16384)

    def test_huge_single_record_string_and_utf8_byte_boundaries(self):
        body = ('{"record":"' + self.noise(90000) + '—α中文🙂", "lexical":-0.000e+99}\n').encode('utf-8')
        paths = partition_json_bytes(body, 'decisions.json', self.root / 'out', max_bytes=16384)
        self.assertGreater(len(paths), 3)
        self.assert_batch(body, paths, 16384)

    def test_same_basename_distinct_snapshots_no_collisions(self):
        a = partition_json_bytes(b'{"a":1}', 'patch-decisions.json', self.root)
        b = partition_json_bytes(b'{"a":2}', 'patch-decisions.json', self.root)
        self.assertFalse(set(a) & set(b))
        self.assertEqual(reassemble_json_parts(a[-1]), b'{"a":1}')
        self.assertEqual(reassemble_json_parts(b[-1]), b'{"a":2}')

    def test_corrupt_part_rejected_and_never_overwritten(self):
        body = json.dumps({'large': self.noise(40000)}).encode()
        paths = partition_json_bytes(body, 'audit', self.root, max_bytes=8192)
        original = paths[0].read_bytes()
        paths[0].write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        corrupt = paths[0].read_bytes()
        with self.assertRaises(ValueError):
            reassemble_json_parts(paths[-1])
        with self.assertRaises(FileExistsError):
            partition_json_bytes(body, 'audit', self.root, max_bytes=8192)
        self.assertEqual(paths[0].read_bytes(), corrupt)

    def test_manifest_wrong_offsets_and_expected_hash_rejected(self):
        body = json.dumps({'large': self.noise(22000)}).encode()
        paths = partition_json_bytes(body, 'audit', self.root, max_bytes=8192)
        with self.assertRaises(ValueError):
            reassemble_json_parts(paths[-1], expected_sha256='0'*64)
        manifest = json.loads(gzip.decompress(paths[-1].read_bytes()))
        manifest['parts'][1]['offset'] += 1
        paths[-1].write_bytes(gzip.compress(json.dumps(manifest).encode(), mtime=0))
        with self.assertRaises(ValueError):
            reassemble_json_parts(paths[-1])

    def test_invalid_json_constants_and_limits_fail_before_writing(self):
        out = self.root / 'out'
        for body in [b'{broken', b'{"n":NaN}', b'{"n":Infinity}']:
            with self.assertRaises(ValueError):
                partition_json_bytes(body, 'audit', out)
        self.assertFalse(out.exists())
        with self.assertRaises(ValueError):
            partition_json_bytes(b'{}', 'audit', out, max_bytes=100)
        self.assertFalse(out.exists())

    def test_manifest_confinement_and_bounded_decompression(self):
        paths = partition_json_bytes(b'{"a":1}', 'audit', self.root)
        original = json.loads(gzip.decompress(paths[-1].read_bytes()))
        for name in ['../outside.json.gz', 'a\\b.json.gz']:
            manifest = dict(original, parts=[dict(original['parts'][0], filename=name)])
            paths[-1].write_bytes(gzip.compress(json.dumps(manifest).encode(), mtime=0))
            with self.assertRaisesRegex(ValueError, 'filename'):
                reassemble_json_parts(paths[-1])
        paths[-1].write_bytes(gzip.compress(json.dumps(original).encode(), mtime=0))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            reassemble_json_parts(paths[-1], max_uncompressed_bytes=1)
        bomb = gzip.compress(b' ' * 100000, mtime=0)
        paths[0].write_bytes(bomb)
        original['parts'][0]['file_bytes'] = len(bomb)
        original['parts'][0]['file_sha256'] = hashlib.sha256(bomb).hexdigest()
        original['parts'][0]['fragment_bytes'] = len(bomb)
        original['parts'][0]['fragment_sha256'] = hashlib.sha256(bomb).hexdigest()
        original['compressed_stream_bytes'] = len(bomb)
        original['compressed_stream_sha256'] = hashlib.sha256(bomb).hexdigest()
        paths[-1].write_bytes(gzip.compress(json.dumps(original).encode(), mtime=0))
        with self.assertRaisesRegex(ValueError, 'bounded decompression'):
            reassemble_json_parts(paths[-1])

    def test_v2_single_compression_retains_repeated_json_efficiency_and_versions(self):
        # Audit-like shared evidence occurs hundreds of times. Raw-JSON base64
        # splitting destroys much of gzip's repetition savings.
        text=self.noise(18000)
        body=json.dumps({'claims':[{'quote':text,'claim':i} for i in range(40)]}).encode()
        paths=partition_json_bytes(body,'audit',self.root,max_bytes=8192)
        manifest=json.loads(gzip.decompress(paths[-1].read_bytes()))
        stream=reassemble_gzip_parts(paths[-1])
        self.assertLess(sum(p.stat().st_size for p in paths), len(stream)*1.15+4096)
        self.assertEqual(gzip.decompress(stream),body)
        self.assertEqual(manifest['format'],'cmccdb-lossless-json-partition-v2')
        self.assertTrue(all('.v2.' in p.name for p in paths))
        another=partition_json_bytes(body,'audit',self.root,max_bytes=16384)
        self.assertFalse(set(paths)&set(another))
        source=self.root/'audit.json.gz';source.write_bytes(gzip.compress(body,mtime=123))
        alternate=partition_audit(source,self.root,max_bytes=8192)
        self.assertFalse(set(paths)&set(alternate))
        self.assertEqual(reassemble_gzip_parts(alternate[-1]),source.read_bytes())

    def test_v2_stream_hash_and_original_json_hash_are_both_required(self):
        paths=partition_json_bytes(json.dumps({'quote':self.noise(30000)}).encode(),'audit',self.root,max_bytes=8192)
        original=json.loads(gzip.decompress(paths[-1].read_bytes()))
        for key in ['compressed_stream_sha256','original_body_sha256']:
            manifest=dict(original);manifest[key]='0'*64
            paths[-1].write_bytes(gzip.compress(json.dumps(manifest).encode(),mtime=0))
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                reassemble_json_parts(paths[-1])

    def test_legacy_v1_raw_and_fragment_envelopes_remain_decodable(self):
        body=b'{ "same":1,"same":2,"n":1.00e+07,"text":"old source" }\n'
        sha=lambda data:hashlib.sha256(data).hexdigest()
        for fragmented in [False,True]:
            fragments=[body[:23],body[23:]] if fragmented else [body]
            rows=[];offset=0
            for number,fragment in enumerate(fragments):
                filename=f'legacy-{fragmented}-{number}.json.gz'
                if fragmented:
                    encoded=gzip.compress(json.dumps(dict(format='cmccdb-json-byte-fragment-v1',offset=offset,
                        body_fragment_bytes=len(fragment),body_fragment_sha256=sha(fragment),encoding='base64',
                        payload=base64.b64encode(fragment).decode())).encode(),mtime=0)
                else:encoded=gzip.compress(fragment,mtime=0)
                (self.root/filename).write_bytes(encoded)
                rows.append(dict(filename=filename,offset=offset,body_fragment_bytes=len(fragment),
                    body_fragment_sha256=sha(fragment),encoding='base64-envelope' if fragmented else 'raw-json-body',
                    file_bytes=len(encoded),file_sha256=sha(encoded)))
                offset+=len(fragment)
            manifest=dict(format='cmccdb-lossless-json-partition-v1',original_body_bytes=len(body),
                original_body_sha256=sha(body),max_file_bytes=8192,part_count=len(rows),parts=rows)
            path=self.root/f'legacy-{fragmented}.manifest.json.gz'
            path.write_bytes(gzip.compress(json.dumps(manifest).encode(),mtime=0))
            self.assertEqual(reassemble_json_parts(path),body)
            with self.assertRaisesRegex(ValueError,'Legacy v1'):
                reassemble_gzip_parts(path)


if __name__ == '__main__':
    unittest.main()
