"""Meaningful structure/coverage/review tests; no scientific corpus is fabricated."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from cmccdb_extraction.chemistry import inspect_smiles, compound_report, chemistry_report, formula_counts
from cmccdb_extraction.pipeline import Pipeline
from cmccdb_extraction.schema import canonical, schema_hash, normalize_claim
from cmccdb_schema.proto import reaction_pb2
from cmccdb_schema import units


def check(status='approved', reason='Unit fixture reviewer checked the cited identity.'):
    return dict(status=status, reason=reason)


def compound_review(path='/inputs/a/components/0', status='resolved_molecular'):
    return dict(compound_path=path, status=status, reason='Explicit unit fixture chemical review.',
                evidence=[dict(evidence_id='fixture:p0001:l0001', quote='Fixture ethanol control')], claim_ids=['fixture-claim'],
                checks={k:check() for k in ['identity','connectivity','formula','charge','stereochemistry','salt_hydrate','coordination']},
                expected_formula='', expected_formal_charge=None, required_stereo_centers=0, required_stereo_bonds=0,
                crystal_reference_status='not_applicable', exception_components_resolved=False)


class StructureTests(unittest.TestCase):
    def report(self, smiles, **review_options):
        compound=dict(identifiers=[dict(type='NAME',value='Fixture'), dict(type='SMILES',value=smiles)])
        review=compound_review();review.update(review_options)
        return compound_report('/inputs/a/components/0', compound, review)

    def test_invalid_and_unsanitizable_smiles_block(self):
        for value in ['C1CC', 'C(C)(C)(C)(C)C', 'CCO trailing prose']:
            with self.subTest(value=value):self.assertFalse(self.report(value)['ready'])
        self.assertTrue(self.report('CCO')['ready'])

    def test_real_salts_hydrates_charge_and_formula(self):
        parsed=inspect_smiles('[Na+].[Cl-].O');self.assertTrue(parsed['valid']);self.assertEqual(parsed['fragments'],3)
        self.assertEqual(parsed['formal_charge'],0)
        self.assertTrue(self.report('[Na+].[Cl-].O',expected_formula='NaCl.H2O',expected_formal_charge=0)['ready'])
        self.assertFalse(self.report('[Na+]',expected_formal_charge=0)['ready'])
        self.assertFalse(self.report('CCO',expected_formula='C2H4O')['ready'])
        self.assertEqual(formula_counts('Cu(CH3COO)2.2H2O'),dict(Cu=1,C=4,H=10,O=6))

    def test_required_source_stereochemistry_and_enantiomer_disagreement(self):
        self.assertFalse(self.report('CC(O)C(=O)O',required_stereo_centers=1)['ready'])
        self.assertTrue(self.report('C[C@H](O)C(=O)O',required_stereo_centers=1)['ready'])
        self.assertFalse(self.report('CC=CC',required_stereo_bonds=1)['ready'])
        self.assertTrue(self.report('C/C=C/C',required_stereo_bonds=1)['ready'])
        compound=dict(identifiers=[dict(type='SMILES',value='C[C@H](O)C(=O)O'),dict(type='SMILES',value='C[C@@H](O)C(=O)O')])
        self.assertFalse(compound_report('/inputs/a/components/0',compound,compound_review())['ready'])

    def test_rdkit_valid_does_not_approve_identity(self):
        report=compound_report('/inputs/a/components/0',dict(identifiers=[dict(type='SMILES',value='CCO')]))
        self.assertFalse(report['ready']);self.assertTrue(report['structures'][0]['valid'])
        review=compound_review();review['checks']['identity']=check('needs_revision','Source identifies ether, candidate encodes ethanol.')
        self.assertFalse(compound_report('/inputs/a/components/0',dict(identifiers=[dict(type='SMILES',value='CCO')]),review)['ready'])

    def test_names_only_need_explicit_resolved_exception(self):
        compound=dict(identifiers=[dict(type='NAME',value='Cinnabar')]);review=compound_review(status='periodic_crystal')
        review['crystal_reference_status']='source_not_reported';review['exception_components_resolved']=True
        self.assertFalse(compound_report('/inputs/a/components/0',compound)['ready'])
        self.assertTrue(compound_report('/inputs/a/components/0',compound,review)['ready'])
        self.assertNotIn('crystal_parameters',compound)  # No invented CSD/cell data.
        review['status']='unresolved';self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])
        review['status']='mixture';review['exception_components_resolved']=False
        self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])

    def test_periodic_exception_cannot_create_a_finite_graph_or_csd(self):
        review=compound_review(status='periodic_crystal');review['exception_components_resolved']=True;review['crystal_reference_status']='source_not_reported'
        compound=dict(identifiers=[dict(type='NAME',value='periodic HgS'),dict(type='SMILES',value='[Hg+2].[S-2]')])
        self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])
        compound['identifiers'].pop();review['crystal_reference_status']='present'
        self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])
        compound['crystal_parameters']=dict(type='CUSTOM',details='Fixture reported crystal.',database_identifier=dict(type='CSD',value='1234567'))
        self.assertTrue(compound_report('/inputs/a/components/0',compound,review)['ready'])
        compound['crystal_parameters']['a']=dict(value=-1,units='ANGSTROM')
        self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])

    def test_molecular_coordination_graph_is_checked_but_not_automatically_approved(self):
        value='[NH3]->[Cu+2]<-[NH3]';self.assertTrue(inspect_smiles(value)['valid'])
        review=compound_review();review['checks']['coordination']=check('needs_revision','Source ligand count/oxidation state not established.')
        self.assertFalse(self.report(value,checks=review['checks'])['ready'])

    def test_periodic_formula_tokens_require_source_review_and_explicit_limits(self):
        options=dict(status='periodic_crystal',exception_components_resolved=True,
                     crystal_reference_status='source_not_reported',structure_scope='stoichiometric_formula_unit',
                     representation_limit='Fixture CdSe stoichiometric token only; no lattice bonding or measured ion charges.',
                     expected_formula='CdSe',expected_formal_charge=0)
        self.assertTrue(self.report('[Cd+2].[Se-2]',**options)['ready'])
        self.assertFalse(self.report('[Cd+2].[S-2]',**options)['ready'])
        self.assertFalse(self.report('[Cd+2].[Se-2]',**{**options,'representation_limit':''})['ready'])
        self.assertFalse(self.report('[Cd+2].[Se-2]',**{**options,'expected_formula':''})['ready'])
        self.assertFalse(self.report('[Ni].[Ni].[Ni].[S].[S]',**{**options,'expected_formula':'Ni3S2'})['ready'])
        # Bare As/Sb formal ions carry RDKit radical bookkeeping. The limited
        # composition token does not assert a measured radical electronic state.
        arsenic='[As+3].[As+3].[S-2].[S-2].[S-2]'
        self.assertGreater(inspect_smiles(arsenic)['radical_electrons'],0)
        self.assertTrue(self.report(arsenic,**{**options,'expected_formula':'As2S3',
                       'representation_limit':'Source orpiment stoichiometric formal ions only; no spin or periodic bonds asserted.'})['ready'])
        self.assertFalse(self.report(arsenic,**{**options,'status':'unresolved','expected_formula':'As2S3'})['ready'])
        self.assertFalse(self.report('[O-2].[O-2].[Si+4]',**{**options,'status':'unresolved','expected_formula':'SiO2'})['ready'])
        self.assertTrue(self.report('[Se]',**{**options,'structure_scope':'elemental_composition','expected_formula':'Se'})['ready'])
        self.assertFalse(self.report('[Se].[Se]',**{**options,'structure_scope':'elemental_composition','expected_formula':'Se2'})['ready'])
        self.assertEqual(inspect_smiles('CC1(C)CCCC(C)(C)N1[O]')['radical_electrons'],1)

    def test_wildcard_graph_is_unresolved(self):
        self.assertTrue(inspect_smiles('C*')['valid']);self.assertFalse(self.report('C*')['ready'])

    def test_identifier_array_hole_blocks_without_crash_or_compaction(self):
        compound=dict(identifiers=[None,dict(type='SMILES',value='CCO')])
        report=compound_report('/inputs/a/components/0',compound,compound_review())
        self.assertFalse(report['ready'])
        self.assertTrue(any('Incomplete identifier slot' in error for error in report['errors']))
        self.assertEqual(report['structures'][0]['identifier_index'],1)
        self.assertIsNone(compound['identifiers'][0])
        self.assertEqual(len(compound['identifiers']),2)
        self.assertFalse(compound_report('/inputs/a/components/0',dict(identifiers=None),compound_review())['ready'])

    def test_nonmolecular_liquid_mercury_is_composition_only(self):
        options=dict(status='nonmolecular_material',exception_components_resolved=True,
                     crystal_reference_status='not_applicable',structure_scope='elemental_composition',
                     representation_limit='Source liquid mercury; [Hg] denotes elemental composition only, with no discrete molecular or lattice bonding claim.',
                     expected_formula='Hg',expected_formal_charge=0)
        self.assertTrue(self.report('[Hg]',**options)['ready'])
        for value in ['[Hg+2]','[Hg].[Hg]','O','[Se]']:
            self.assertFalse(self.report(value,**options)['ready'])
        self.assertFalse(self.report('[Hg]',**{**options,'representation_limit':''})['ready'])
        self.assertFalse(self.report('[Hg]',**{**options,'structure_scope':'molecular_graph'})['ready'])
        self.assertFalse(self.report('[Hg]',**{**options,'exception_components_resolved':False})['ready'])
        compound=dict(identifiers=[dict(type='SMILES',value='[Hg]')],crystal_parameters=dict(database_identifier=dict(type='CSD',value='fixture')))
        review=compound_review();review.update(options)
        self.assertFalse(compound_report('/inputs/a/components/0',compound,review)['ready'])

    def test_optional_product_crystal_tag_and_angstrom_conversions(self):
        self.assertEqual(reaction_pb2.ProductCompound.DESCRIPTOR.fields_by_name['crystal_parameters'].number,100)
        self.assertEqual(reaction_pb2.Compound.DESCRIPTOR.fields_by_name['crystal_parameters'].number,100)
        self.assertEqual(reaction_pb2.Length.ANGSTROM,6)
        value=normalize_claim('/outcomes/0/products/0/crystal_parameters/a',dict(value=5.4,units='Å'))
        self.assertEqual(value,dict(value=5.4,units='ANGSTROM'))
        resolver=units.UnitResolver();cm=resolver.convert(reaction_pb2.Length(value=10,units=reaction_pb2.Length.ANGSTROM),reaction_pb2.Length.CENTIMETER)
        self.assertAlmostEqual(cm.value,1e-7,places=14)
        ang=resolver.convert(cm,reaction_pb2.Length.ANGSTROM);self.assertAlmostEqual(ang.value,10,places=5)
        # Ball-size representation is preserved in centimeters.
        self.assertEqual(normalize_claim('/conditions/mechanochemistry/ball_radius',dict(value=4,units='MILLIMETER')),dict(value=0.4,units='CENTIMETER'))


class ReviewLifecycleTests(unittest.TestCase):
    """Exercise persisted Pipeline review, stale claims, coverage and export gate."""
    def setUp(self):
        root=Path(__file__).resolve().parents[4]/'structure-repair-20261007/test-runs';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root);self.pipeline=Pipeline([self.tmp.name],Path(self.tmp.name)/'state')
        self.run='run-fixture';self.line=dict(evidence_id='fixture:p0001:l0001',kind='text',text='Fixture ethanol control')
        self.patch=patch.object(self.pipeline,'evidence',side_effect=lambda run,ids:[self.line for _ in ids]);self.patch.start()
        manifest=dict(run_id=self.run,dataset_id='cmcc_dataset-'+'0'*32,dataset_name='Chemistry unit fixture',sources=[],schema_sha256=schema_hash(),created_at=datetime.now(timezone.utc).isoformat(),curator=dict(name='Development Test Curator',email='unit@example.invalid'))
        with self.pipeline.store.connect(write=True) as con:con.execute('INSERT INTO runs VALUES(?,?,?)',(self.run,None,canonical(manifest)))
        plan=dict(experiments=[dict(key='fixture',label='Unit fixture control',evidence_ids=[self.line['evidence_id']],scope='Synthetic software-test data; not a scientific extraction.')],tasks=[dict(task_id='identity',experiment_keys=['fixture'],allowed_paths=['/inputs','/outcomes'],evidence_ids=[self.line['evidence_id']],instructions='Unit fixture data',required_workers=2)],source_search_complete=True)
        self.pipeline.set_plan(self.run,plan)
        claims=[]
        for path,value in [('/inputs/a/components/0',dict(identifiers=[dict(type='NAME',value='ethanol'),dict(type='SMILES',value='CCO')],reaction_role='REACTANT',amount=dict(unmeasured=dict(type='CUSTOM',details='Unit fixture unmeasured charge.')))),('/outcomes/0/products/0/identifiers/0',dict(type='NAME',value='ethanol')),('/outcomes/0/products/0/identifiers/1',dict(type='SMILES',value='CCO')),('/outcomes/0/products/0/reaction_role','PRODUCT')]:
            claims.append(dict(experiment_key='fixture',path=path,value=value,evidence=[dict(evidence_id=self.line['evidence_id'],quote='Fixture ethanol control')],basis='reported',source_value='Synthetic unit fixture ethanol control.'))
        for worker in ['a','b']:
            packet=self.pipeline.lease(self.run,worker,'unit-fixture-model','fixture')
            self.pipeline.submit(self.run,'identity',worker,packet['lease_token'],dict(claims=claims))

    def tearDown(self):self.patch.stop();self.tmp.cleanup()

    def test_completed_workers_are_public_task_scoped_ids_only(self):
        with self.pipeline.store.connect(write=True) as con:
            # Other-run and nonexistent-task assignments cannot contaminate status.
            con.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?)",('other-run','identity','foreign','fixture','fixture','private-token',0,1,'private-payload'))
            con.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?)",(self.run,'missing-task','orphan','fixture','fixture','private-token',0,1,'private-payload'))
            con.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?)",(self.run,'identity','leased','fixture','fixture','private-token',0,0,None))
        status=self.pipeline.status(self.run)
        self.assertEqual(len(status['tasks']),1)
        self.assertEqual(status['tasks'][0]['completed_workers'],['a','b'])
        self.assertTrue(status['tasks'][0]['completed'])
        self.assertNotIn('private-token',canonical(status))
        self.assertNotIn('private-payload',canonical(status))
        restarted=Pipeline([self.tmp.name],Path(self.tmp.name)/'state')
        self.assertEqual(restarted.status(self.run)['tasks'][0]['completed_workers'],['a','b'])

    def test_invalid_identifier_assembly_returns_validation_before_text_flatten(self):
        consensus=copy.deepcopy(self.pipeline.consensus(self.run))
        component=next(f for f in consensus['fields'] if f['path']=='/inputs/a/components/0')
        component['value']['identifiers'][0]=None
        with patch.object(self.pipeline,'consensus',return_value=consensus), patch('cmccdb_extraction.export.export_text_boundaries') as boundary:
            assembled=self.pipeline.assemble(self.run)
        boundary.assert_not_called()
        self.assertFalse(assembled['validation']['valid'])
        self.assertTrue(assembled['validation']['errors'])
        self.assertFalse(assembled['validation']['chemistry_ready'])
        self.assertIsNone(assembled['dataset']['reactions'][0]['inputs']['a']['components'][0]['identifiers'][0])
        self.assertEqual(len(assembled['dataset']['reactions'][0]['inputs']['a']['components'][0]['identifiers']),2)

    def current_review(self):
        report=self.pipeline.assemble(self.run)['chemistry_report']['reactions'][0]
        items=[]
        for compound in report['compounds']:
            item=compound_review(compound['compound_path']);item['claim_ids']=compound['selected_identity_claim_ids'];item['expected_formula']='C2H6O';item['expected_formal_charge']=0;items.append(item)
        return dict(experiment_key='fixture',**{k:report[k] for k in ['dataset_sha256','reaction_sha256','claims_sha256']},actor='Development Test Curator',disposition='approved',rationale='Synthetic unit fixture chemistry checked; no scientific assertion.',evidence=[dict(evidence_id=self.line['evidence_id'],quote='Fixture ethanol control')],reaction_plausibility=check(),measurement_semantics=check(),compounds=items)

    def test_missing_review_blocks_normal_export_but_partial_draft_remains(self):
        assembled=self.pipeline.assemble(self.run);self.assertTrue(assembled['validation']['valid']);self.assertFalse(assembled['validation']['chemistry_ready'])
        with self.assertRaisesRegex(ValueError,'not ready'):self.pipeline.export(self.run)
        with patch('cmccdb_extraction.export.export_run',return_value={'draft':True}) as export:
            self.assertEqual(self.pipeline.export(self.run,allow_partial=True),{'draft':True});self.assertTrue(export.call_args.args[-1])

    def test_complete_review_persists_audit_and_does_not_invent_operator(self):
        result=self.pipeline.review_chemistry(self.run,self.current_review());self.assertTrue(result['chemistry_ready'])
        assembled=self.pipeline.assemble(self.run);self.assertTrue(assembled['validation']['chemistry_ready'])
        self.assertFalse(assembled['validation']['ready_for_contribution'])  # Fixtures can never contribute.
        self.assertEqual(len(self.pipeline.store.audit(self.run)['chemistry_reviews']),1)
        self.assertNotIn('experimenter',assembled['dataset']['reactions'][0]['provenance'])
        restarted=Pipeline([self.tmp.name],Path(self.tmp.name)/'state');self.assertEqual(len(restarted.store.audit(self.run)['chemistry_reviews']),1)

    def test_review_must_cover_all_compounds_and_current_claim_ids(self):
        review=self.current_review();review['compounds'].pop()
        with self.assertRaisesRegex(ValueError,'exactly every'):self.pipeline.review_chemistry(self.run,review)
        review=self.current_review();review['compounds'][0]['claim_ids']=['unrelated']
        with self.assertRaisesRegex(ValueError,'current selected'):self.pipeline.review_chemistry(self.run,review)
        review=self.current_review();review['compounds'][0]['expected_formula']='C3H8O'
        with self.assertRaisesRegex(ValueError,'formula'):self.pipeline.review_chemistry(self.run,review)

    def test_stale_claim_selection_and_dataset_changes_invalidate_review(self):
        review=self.current_review();self.pipeline.review_chemistry(self.run,review)
        field=self.pipeline.consensus(self.run)['fields'][0];different=next(c for c in field['candidates'] if c['claim_id']!=field['selected_claim_id'])
        self.pipeline.resolve(self.run,'fixture',field['path'],different['claim_id'],'Unit fixture','Select the other equivalent candidate to test claim binding.')
        self.assertFalse(self.pipeline.assemble(self.run)['validation']['chemistry_ready'])
        with self.assertRaisesRegex(ValueError,'stale'):self.pipeline.review_chemistry(self.run,review)
        fresh=self.current_review();self.pipeline.review_chemistry(self.run,fresh)
        assembled=self.pipeline.assemble(self.run);mutated=copy.deepcopy(assembled['dataset']);mutated['name']='Changed dataset'
        reviews={r['experiment']:r['data'] for r in self.pipeline.store.audit(self.run)['chemistry_reviews']}
        self.assertFalse(chemistry_report(mutated,assembled['consensus']['fields'],{mutated['reactions'][0]['reaction_id']:'fixture'},reviews)['ready'])

    def test_whole_crystal_claim_is_required_and_reviewed_export_text_matches(self):
        # New explicitly synthetic source claim added to both immutable fixture submissions
        # through a separate task, without mutating earlier claims.
        original = self.pipeline.consensus(self.run)["fields"]
        crystal_path = "/outcomes/0/products/0/crystal_parameters"
        with self.pipeline.store.connect(write=True) as con:
            task = dict(task_id="crystal", experiment_keys=["fixture"], allowed_paths=[crystal_path],
                        evidence_ids=[self.line["evidence_id"]], instructions="Synthetic source cell fixture", required_workers=2, depends_on=[], modality="text")
            con.execute("INSERT INTO tasks VALUES(?,?,?)", (self.run,"crystal",canonical(task)))
        claim = dict(experiment_key="fixture", path=crystal_path,
                     value=dict(type="CUSTOM",details="  Synthetic unit fixture crystal metadata.  ",database_identifier=dict(type="CSD",value="1234567")),
                     evidence=[dict(evidence_id=self.line["evidence_id"],quote="Fixture ethanol control")],basis="reported",source_value="Synthetic fixture crystal metadata.")
        for worker in ["a","b"]:
            packet=self.pipeline.lease(self.run,worker,"unit-fixture-model","fixture",task_id="crystal")
            self.pipeline.submit(self.run,"crystal",worker,packet["lease_token"],dict(claims=[claim]))
        assembled=self.pipeline.assemble(self.run)
        self.assertEqual(assembled["dataset"]["reactions"][0]["outcomes"][0]["products"][0]["crystal_parameters"]["details"],"Synthetic unit fixture crystal metadata.")
        self.assertTrue(assembled["text_boundary_normalizations"])
        review=self.current_review();product=next(c for c in review["compounds"] if c["compound_path"].startswith("/outcomes/"));product["crystal_reference_status"]="present"
        selected=next(f["selected_claim_id"] for f in self.pipeline.consensus(self.run)["fields"] if f["path"]==crystal_path)
        self.assertIn(selected,product["claim_ids"])
        removed=copy.deepcopy(review);next(c for c in removed["compounds"] if c["compound_path"].startswith("/outcomes/"))["claim_ids"].remove(selected)
        with self.assertRaisesRegex(ValueError,"current selected"):self.pipeline.review_chemistry(self.run,removed)
        self.pipeline.review_chemistry(self.run,review)
        assembled=self.pipeline.assemble(self.run);self.assertTrue(assembled["validation"]["chemistry_ready"])
        from cmccdb_extraction.export import export_text_boundaries
        actual,changes=export_text_boundaries(assembled["dataset"]);self.assertEqual(changes,[])
        reviews={r["experiment"]:r["data"] for r in self.pipeline.store.audit(self.run)["chemistry_reviews"]}
        self.assertTrue(chemistry_report(actual,assembled["consensus"]["fields"],{actual["reactions"][0]["reaction_id"]:"fixture"},reviews)["ready"])

    def test_review_source_quotes_cannot_be_invented(self):
        review=self.current_review();review['compounds'][0]['evidence'][0]['quote']='Invented CSD entry'
        with self.assertRaisesRegex(ValueError,'does not occur'):self.pipeline.review_chemistry(self.run,review)

    def test_atomic_adjudication_rolls_back_decisions_and_audit_events(self):
        fields=self.pipeline.consensus(self.run)['fields']
        first,second=fields[:2]
        self.pipeline.resolve(self.run,'fixture',first['path'],first['candidates'][0]['claim_id'],'Unit fixture','Prior decision preserved on rollback.')
        before=self.pipeline.store.audit(self.run)
        items=[dict(experiment_key='fixture',path=f['path'],claim_id=f['candidates'][-1]['claim_id'],actor='Unit fixture',reason='Explicit unit fixture batch choice.') for f in [first,second]]
        items[-1]['claim_id']=first['candidates'][0]['claim_id']  # Existing ID, wrong field, after one valid write.
        with self.assertRaisesRegex(ValueError,'does not belong'):self.pipeline.resolve_many(self.run,items)
        after=self.pipeline.store.audit(self.run)
        self.assertEqual(before['decisions'],after['decisions']);self.assertEqual(before['events'],after['events'])
        items[-1]['claim_id']=second['candidates'][-1]['claim_id']
        self.assertEqual(self.pipeline.resolve_many(self.run,items),dict(adjudicated=2,atomic=True))
        after=self.pipeline.store.audit(self.run)
        self.assertEqual(len(after['decisions']),2);self.assertEqual(len(after['events'])-len(before['events']),2)
        with self.assertRaisesRegex(ValueError,'only once'):self.pipeline.resolve_many(self.run,[items[0],items[0]])
        for invalid in [[],items*251,[{**items[0],'claim_id':None}],[{**items[0],'actor':' '}]]:
            with self.assertRaises(ValueError):self.pipeline.resolve_many(self.run,invalid)


if __name__=='__main__':unittest.main()
