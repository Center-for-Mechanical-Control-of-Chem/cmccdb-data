"""Oversized full audits remain complete in every independently checked XLSX part."""
import base64
import copy
import gzip
import hashlib
import json
import random
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cmccdb_extraction.audit_transport import (
    prepare_audit_transport, audit_documentation_rows, exported_resource_path)
from cmccdb_extraction.auxiliary_partition import reassemble_json_parts
from cmccdb_extraction.export import export_run, workbook_plan
from cmccdb_extraction.schema import canonical, dataset_from_json
from cmccdb_schema.dataset_constructor import DatasetConstructor
from test_export import fixture_reaction, TEST_ROOT


def fixture(directory):
    audit = dict(experiments=[dict(reaction_id=fixture_reaction(i)['reaction_id'], key=f'condition-{i}',
                                  data=dict(label='Format fixture')) for i in range(1, 3)],
                 manifest=dict(schema_sha256='fixture', sources=[]))
    fields = [dict(experiment_key=f'condition-{i}', path='/conditions/mechanochemistry', candidates=[], status='agreed')
              for i in range(1, 3)]
    assembled = dict(dataset=dict(dataset_id='cmcc_dataset-'+'d'*32, name='Transport format fixture',
                                 reactions=[fixture_reaction(i) for i in range(1, 3)]),
                     consensus=dict(fields=fields), review=[],
                     validation=dict(ready_for_contribution=True, errors=[], warnings=[], incomplete_tasks=[], coverage_notes=[]))
    store = SimpleNamespace(run_dir=lambda run:directory, audit=lambda run:audit,
                            connect=lambda **kwargs:nullcontext(None), event=lambda *args:None)
    return SimpleNamespace(store=store, node='fixture-backend'), audit, assembled


class AuditTransportTests(unittest.TestCase):
    def test_oversized_audit_archive_is_not_an_upload_attachment(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory = Path(temp)
            body = canonical({'claims':base64.b64encode(random.Random(2).randbytes(100000)).decode()}).encode()
            result = prepare_audit_transport(directory, body, max_bytes=8192)
            self.assertGreater((directory/'extraction-audit.json.gz').stat().st_size,8192)
            self.assertNotIn('extraction-audit.json.gz',result['files'])
            self.assertEqual(reassemble_json_parts(directory/result['manifest']['filename']),body)
            self.assertEqual(set(audit_documentation_rows(result)),{'Overview','Review','Schema'})
            for name in result['files']:
                self.assertLessEqual((directory/name).stat().st_size,8192)
                json.loads(gzip.decompress((directory/name).read_bytes()))

    def test_whole_record_parts_carry_identical_complete_audit_and_auxiliaries(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory = Path(temp);pipeline,audit,assembled = fixture(directory)
            audit['all_claims_including_withheld'] = base64.b64encode(random.Random(4).randbytes(100000)).decode()
            auxiliary = directory/'source-manager-audit.json.gz';auxiliary.write_bytes(b'immutable full auxiliary')
            def backend(args, **kwargs):
                revision = Path(args[-1]).parent
                data = json.loads((revision/'dataset.json').read_text())
                # Force splitting; this isolates the size/receipt policy from Artifact authoring,
                # which has separate real backend and strict converter integration tests.
                (revision/'extraction.xlsx').write_bytes(b'x'*(9000 if len(data['reactions'])>1 else 100))
                return subprocess.CompletedProcess(args,0,'','')
            def converter(path, **kwargs):
                return dataset_from_json(json.loads((Path(path).parent/'dataset.json').read_text()))
            limited = lambda directory,body,**kwargs:prepare_audit_transport(directory,body,max_bytes=8192,**kwargs)
            with patch('cmccdb_extraction.export.prepare_audit_transport',side_effect=limited), \
                 patch('cmccdb_extraction.export.MAX_ATTACHMENT_BYTES',8192), \
                 patch('cmccdb_extraction.export.subprocess.run',side_effect=backend), \
                 patch.object(DatasetConstructor,'enumerate_spreadsheet',side_effect=converter):
                result = export_run(pipeline,'fixture-run',assembled,False,auxiliary_files=[str(auxiliary)])
            self.assertTrue(result['transport_ready']);self.assertFalse(result['upload_size_checks_passed'])
            self.assertEqual(len(result['transport_parts']),2)
            ids = [i for part in result['transport_parts'] for i in part['reaction_ids']]
            self.assertEqual(set(ids),{r['reaction_id'] for r in assembled['dataset']['reactions']})
            self.assertEqual(len(ids),len(set(ids)))
            for item in [result,*result['transport_parts']]:
                revision = Path(item['workbook']).parent
                self.assertTrue(item['round_trip_verified'])
                self.assertEqual(item['audit_transport_files'],result['audit_transport_files'])
                self.assertEqual(item['auxiliary_files'],result['auxiliary_files'])
                self.assertNotIn('extraction-audit.json.gz',item['auxiliary_files'])
                self.assertEqual((revision/auxiliary.name).read_bytes(),b'immutable full auxiliary')
                self.assertEqual(reassemble_json_parts(revision/item['audit_transport_manifest']['filename']),canonical(audit).encode())
                plan = json.loads((revision/'workbook-plan.json').read_text())
                for sheet in plan['sheets']:
                    if sheet['name'] in {'Overview','Review','Schema'}:
                        self.assertEqual(sheet['rows'][-1][1],'Audit transport')
                for name in item['auxiliary_files']:
                    body = (revision/name).read_bytes()
                    self.assertLessEqual(len(body),8192)
                    self.assertEqual(hashlib.sha256(body).hexdigest(),item['auxiliary_sha256'][name])

    def test_snapshot_reuse_requires_exact_body_and_immutable_fragment_hashes(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory=Path(temp);a=directory/'a';a.mkdir();b=directory/'b';b.mkdir()
            body=canonical({'claims':base64.b64encode(random.Random(8).randbytes(50000)).decode()}).encode()
            snapshot=prepare_audit_transport(a,body,max_bytes=8192)
            reused=prepare_audit_transport(b,body,max_bytes=8192,snapshot=snapshot)
            self.assertEqual(reused,snapshot)
            with self.assertRaisesRegex(ValueError,'does not match'):
                prepare_audit_transport(b,b'{}',max_bytes=8192,snapshot=snapshot)
            corrupt=copy.deepcopy(snapshot);name=corrupt['files'][0];corrupt['blobs'][name]=b'corrupt'
            with self.assertRaisesRegex(ValueError,'snapshot file'):
                prepare_audit_transport(b,body,max_bytes=8192,snapshot=corrupt)

    def test_completed_receipt_controls_auxiliary_resource_access(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            root=Path(temp);directory=root/'revision-fixture';directory.mkdir()
            part=directory/'audit.part-00001.json.gz';part.write_bytes(b'part')
            with self.assertRaisesRegex(ValueError,'incomplete'):
                exported_resource_path(directory,part.name)
            (directory/'receipt.json').write_text(canonical(dict(auxiliary_files=[part.name])))
            self.assertEqual(exported_resource_path(directory,part.name),part)
            unlisted=directory/'unlisted.json';unlisted.write_text('{}')
            for name in [unlisted.name,'../receipt.json','a\\b.json','.hidden.json']:
                with self.assertRaises(ValueError):exported_resource_path(directory,name)
            part.unlink();outside=root/'outside.json.gz';outside.write_bytes(b'outside');part.symlink_to(outside)
            with self.assertRaisesRegex(ValueError,'escapes'):
                exported_resource_path(directory,part.name)

    def test_aggregate_budget_splits_workbook_even_when_each_file_fits(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory=Path(temp);pipeline,audit,assembled=fixture(directory)
            audit['source_quotes']=base64.b64encode(random.Random(10).randbytes(6000)).decode()
            aux=directory/'review-extra.json.gz';aux.write_bytes(b'auxiliary'*100)
            def backend(args,**kwargs):
                path=Path(args[-1]);data=json.loads((path.parent/'dataset.json').read_text())
                path.write_bytes(b'x'*(5000 if len(data['reactions'])>1 else 1000))
                return subprocess.CompletedProcess(args,0,'','')
            def converter(path,**kwargs):return dataset_from_json(json.loads((Path(path).parent/'dataset.json').read_text()))
            with patch('cmccdb_extraction.export.MAX_REQUEST_BYTES',10000), \
                 patch('cmccdb_extraction.export.subprocess.run',side_effect=backend), \
                 patch.object(DatasetConstructor,'enumerate_spreadsheet',side_effect=converter):
                result=export_run(pipeline,'fixture-run',assembled,False,auxiliary_files=[str(aux)])
            self.assertGreater(result['upload_request_bytes'],10000)
            self.assertEqual(len(result['transport_parts']),2)
            self.assertTrue(result['transport_ready'])
            for part in result['transport_parts']:
                self.assertLessEqual(part['upload_request_bytes'],10000)
                self.assertEqual(part['shared_auxiliary_bytes'],result['shared_auxiliary_bytes'])

    def test_complete_shared_auxiliaries_over_budget_fail_before_backend(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory=Path(temp);pipeline,audit,assembled=fixture(directory)
            aux=directory/'large-extra.json.gz';aux.write_bytes(b'x'*12000)
            with patch('cmccdb_extraction.export.MAX_REQUEST_BYTES',10000), \
                 patch('cmccdb_extraction.export.subprocess.run') as backend:
                with self.assertRaisesRegex(ValueError,'before XLSX'):
                    export_run(pipeline,'fixture-run',assembled,False,auxiliary_files=[str(aux)])
            backend.assert_not_called()

    def test_audit_plus_other_attachments_counts_toward_same_64_file_limit(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            directory=Path(temp);pipeline,audit,assembled=fixture(directory)
            files=[]
            for i in range(64):
                path=directory/f'part-{i}.json.gz';path.write_bytes(b'fixture');files.append(str(path))
            with patch('cmccdb_extraction.export.subprocess.run') as backend:
                with self.assertRaisesRegex(ValueError,'64-file'):
                    export_run(pipeline,'fixture-run',assembled,False,auxiliary_files=files)
            backend.assert_not_called()
            from cmccdb_extraction.export import auxiliary_snapshot
            with self.assertRaisesRegex(ValueError,'64'):
                auxiliary_snapshot(directory,[*files,str(directory/'not-read')])

    def test_experiment_note_index_preserves_old_order_and_ancestor_citations(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            pipeline,audit,assembled=fixture(Path(temp))
            fields=[]
            for exp,path,n in [('condition-2','/conditions',1),('condition-1','/conditions',2),
                               ('condition-1','/conditions/mechanochemistry/frequency',3),
                               ('condition-2','/conditions/mechanochemistry',4)]:
                fields.append(dict(experiment_key=exp,path=path,status='agreed',candidates=[dict(
                    experiment_key=exp,path=path,source_value='fixture',normalized_value='fixture',basis='reported',
                    worker_id='fixture',model_revision='fixture',claim_id=str(n),explanation='',
                    evidence=[dict(evidence_id=f'fixture:{n}',quote='fixture',observation='',bbox=None)])]))
            assembled['consensus']['fields']=fields
            plan=workbook_plan(pipeline,'fixture-run',assembled)
            indexed=next(s['notes'] for s in plan['sheets'] if s['name']=='ReactionData')
            from cmccdb_extraction.export import address
            by_id={e['reaction_id']:e['key'] for e in audit['experiments']}
            old=[]
            for cell in plan['data_cells']:
                claims=[]
                for field in fields:
                    if field['experiment_key']==by_id[cell['reaction_id']] and (cell['path']==field['path'] or cell['path'].startswith(field['path']+'/')):
                        claims.extend(field['candidates'])
                if claims:
                    old.append(dict(cell=address(cell['row'],cell['column']),text='\n'.join(
                        f"{c['claim_id']}: "+'; '.join(e['evidence_id'] for e in c['evidence']) for c in claims)))
            self.assertEqual(indexed,old)
