"""Real-SI grounding and lossless text export boundary checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from cmccdb_extraction.contracts import EvidenceRef
from cmccdb_extraction.documents import index_source
from cmccdb_extraction.export import export_text_boundaries
from cmccdb_extraction.pipeline import Pipeline
from cmccdb_extraction.schema import normalize_claim

TASK = Path(os.environ['CMCCDB_RSC_TASK_ROOT']) if os.environ.get('CMCCDB_RSC_TASK_ROOT') else None

@unittest.skipUnless(TASK, 'Set CMCCDB_RSC_TASK_ROOT to the indexed real-SI draft run')
class NativeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet=json.loads((TASK/'papers/d5mr00088b/worker-packet.json').read_text())
        cls.native=next(s for s in cls.packet['sources'] if s['source_format']=='xlsx')
        cls.index=json.loads(Path(cls.native['index']).read_text())
        cls.pipeline=Pipeline([TASK.parent.parent/'cmccdb-data/tests/rsc_mechanochemistry',TASK/'supplemental-sources'],TASK/'runs')

    def test_original_measured_rows_have_exact_cell_addresses(self):
        rows=[r for r in self.index['lines'] if r['sheet']=='Data_meas only' and 6<=r['row']<=59]
        self.assertEqual(len(rows),54)
        self.assertEqual(self.native['pages'],42)
        for r in rows:
            self.assertIn('Data_meas only!A'+str(r['row'])+' = ',r['text'])
            self.assertEqual(r['kind'],'text')
            self.assertIsNone(r['bbox'])
        ref=EvidenceRef(evidence_id=rows[0]['evidence_id'],quote=rows[0]['text'].split(' | ')[0])
        self.assertEqual(self.pipeline.checked_reference(self.packet['run_id'],ref)['row'],6)
        with self.assertRaisesRegex(ValueError,'does not occur'):
            self.pipeline.checked_reference(self.packet['run_id'],EvidenceRef(evidence_id=ref.evidence_id,quote='invented cell value'))

    def test_formula_text_and_existing_caches_remain_distinct(self):
        formulas=[r for r in self.index['lines'] if ' = =' in r['text']]
        self.assertGreater(len(formulas),100)
        self.assertTrue(all('[cached result:' in r['text'] for r in formulas))
        self.assertIn('not executed',self.index['coordinate_system'])
        self.assertIn('not recalculated',self.index['warnings'][0])
        with self.assertRaisesRegex(ValueError,'unavailable'):
            self.pipeline.page(self.packet['run_id'],self.native['source_id'],1)

    def test_tex_is_original_utf8_lines_never_a_pdf_coordinate(self):
        records=json.loads((TASK/'nested-text-si-manifest.json').read_text())
        self.assertTrue(records)
        item=records[0];source=Path(item['source_path']);indexed=index_source(source)
        self.assertEqual(indexed['sha256'],item['sha256'])
        original=source.read_text().splitlines()
        self.assertTrue(indexed['lines'])
        for line in indexed['lines']:
            self.assertEqual(line['text'],original[line['file_line']-1])
            self.assertIsNone(line['bbox'])
        self.assertEqual(indexed['pages'][0]['kind'],'text_file')

class ExportBoundaryTests(unittest.TestCase):
    def test_ram_acceleration_preserves_canonical_unit_and_precision(self):
        result=normalize_claim('/conditions/mechanochemistry/g_force',
                               {'value':80,'precision':0,'units':'STANDARD_GRAVITATIONAL_ACCELERATION'})
        self.assertEqual(result,{'value':80.0,'precision':0.0,'units':'STANDARD_GRAVITATIONAL_ACCELERATION'})
        with self.assertRaisesRegex(ValueError,'incompatible'):
            normalize_claim('/conditions/mechanochemistry/g_force',{'value':80,'units':'gram'})
        result=normalize_claim('/conditions/mechanochemistry/frequency',{'value':600,'units':'RPM'})
        self.assertEqual(result,{'value':10.0,'units':'HERTZ'})

    def test_boundary_normalization_preserves_internal_text_numbers_and_original(self):
        original={'reactions':[{'reaction_id':'cmcc-'+32*'a','observations':[{'comment':'  measured  phase I \n'}],
                               'conditions':{'mechanochemistry':{'frequency':{'value':7.25,'units':'HERTZ'}}}}]}
        normalized,changes=export_text_boundaries(original)
        self.assertEqual(original['reactions'][0]['observations'][0]['comment'],'  measured  phase I \n')
        self.assertEqual(normalized['reactions'][0]['observations'][0]['comment'],'measured  phase I')
        self.assertEqual(normalized['reactions'][0]['conditions'],original['reactions'][0]['conditions'])
        self.assertEqual(changes[0]['path'],'/observations/0/comment')
        self.assertEqual(changes[0]['original'],original['reactions'][0]['observations'][0]['comment'])

    def test_map_key_whitespace_requires_manager_repair(self):
        with self.assertRaisesRegex(ValueError,'manager remapping'):
            export_text_boundaries({'reactions':[{'reaction_id':'cmcc-'+32*'a','inputs':{' accidental key ': {'components':[]}}}]})

if __name__=='__main__':unittest.main()
