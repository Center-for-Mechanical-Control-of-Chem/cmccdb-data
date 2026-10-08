"""Lossless bounded inspection transport, including digest/length tampering."""
import base64
import copy
import gzip
import hashlib
import unittest
from unittest.mock import patch

from cmccdb_extraction.responses import inspection_response, decode_inspection_response
from cmccdb_extraction.schema import canonical


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.payload=dict(fields=[dict(claim_id='source-claim', value='Å and α',
                                     candidates=[dict(value=0.0),dict(value=None)])] * 1000,
                          validation=dict(valid=False,errors=['Unresolved source graph']))

    def test_default_identity_and_deterministic_lossless_envelope(self):
        self.assertIs(inspection_response(self.payload),self.payload)
        packet=inspection_response(self.payload,True)
        self.assertEqual(packet,inspection_response(self.payload,True))
        self.assertEqual(set(packet),{'format','gzip_base64','uncompressed_bytes','json_sha256','gzip_sha256'})
        raw=canonical(self.payload).encode('utf-8');blob=base64.b64decode(packet['gzip_base64'])
        self.assertEqual(gzip.decompress(blob),raw)
        self.assertEqual(packet['uncompressed_bytes'],len(raw))
        self.assertEqual(packet['json_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(packet['gzip_sha256'],hashlib.sha256(blob).hexdigest())
        self.assertLess(len(blob),len(raw)//10)
        self.assertEqual(decode_inspection_response(packet),self.payload)

    def test_tampered_gzip_json_hash_and_length_fail(self):
        packet=inspection_response(self.payload,True)
        changed=copy.deepcopy(packet);blob=bytearray(base64.b64decode(changed['gzip_base64']));blob[-1]^=1
        changed['gzip_base64']=base64.b64encode(blob).decode()
        with self.assertRaisesRegex(ValueError,'gzip SHA256 mismatch'):decode_inspection_response(changed)
        changed={**packet,'json_sha256':'0'*64}
        with self.assertRaisesRegex(ValueError,'JSON SHA256 mismatch'):decode_inspection_response(changed)
        with self.assertRaisesRegex(ValueError,'length mismatch'):decode_inspection_response({**packet,'uncompressed_bytes':packet['uncompressed_bytes']-1})
        with self.assertRaisesRegex(ValueError,'invalid base64'):decode_inspection_response({**packet,'gzip_base64':'!invalid!'})

    def test_size_limits_fail_without_truncation_or_unbounded_decode(self):
        packet=inspection_response(self.payload,True)
        with patch('cmccdb_extraction.responses.MAX_COMPRESSED_BYTES',1):
            with self.assertRaisesRegex(ValueError,'no data was truncated'):inspection_response(self.payload,True)
            with self.assertRaisesRegex(ValueError,'16 MiB'):decode_inspection_response(packet)
        with self.assertRaisesRegex(ValueError,'excessive uncompressed length'):
            decode_inspection_response(packet,max_uncompressed_bytes=10)
        self.assertEqual(len(self.payload['fields']),1000)


if __name__=='__main__':unittest.main()
