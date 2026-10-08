"""Attachments are bounded and snapshotted before workbook transport checks."""
import tempfile
import unittest
from pathlib import Path
from cmccdb_extraction.export import auxiliary_snapshot


class AuxiliaryExportTests(unittest.TestCase):
    def test_confined_snapshot_preserves_bytes_and_rejects_aliases(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd(),prefix='auxiliary-test-') as temp:
            run=Path(temp)/'run';run.mkdir();inside=run/'review.json.gz';inside.write_bytes(b'snapshot')
            blobs=auxiliary_snapshot(run,[str(inside)])
            inside.write_bytes(b'later edit')
            self.assertEqual(blobs,{'review.json.gz':b'snapshot'})
            outside=Path(temp)/'other.json';outside.write_text('{}')
            with self.assertRaisesRegex(ValueError,'inside this run'):
                auxiliary_snapshot(run,[str(outside)])
            alias=run/'alias.json';alias.symlink_to(outside)
            with self.assertRaisesRegex(ValueError,'inside this run'):
                auxiliary_snapshot(run,[str(alias)])
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                auxiliary_snapshot(run,[str(inside),str(inside)])

    def test_limit_and_reserved_names_are_checked_before_export(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd(),prefix='auxiliary-test-') as temp:
            run=Path(temp);path=run/'large.json'
            with path.open('wb') as file:file.truncate(5*1024*1024+1)
            with self.assertRaisesRegex(ValueError,'5 MiB'):
                auxiliary_snapshot(run,[str(path)])
            reserved=run/'dataset.json';reserved.write_text('{}')
            with self.assertRaisesRegex(ValueError,'reserved'):
                auxiliary_snapshot(run,[str(reserved)])
