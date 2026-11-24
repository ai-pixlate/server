"""Synthetic read-only viewer tests; no product imports, DB, inference or network."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from tools.baseline.core import digest
from tools.baseline.viewer import generate


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'input'
        self.root.mkdir()
        self.output = self.base / 'view'
        self.m = {'evaluation_run_id': 'arbitrary-round-27', 'source_keys': [], 'case_inputs': {},
                  'product_version': 'future', 'product_commit': 'sha', 'artifacts': [], 'files': [], 'reference_freeze': None}
        self.write('object-index.json', {})
        self.root.joinpath('execution.jsonl').write_text('', encoding='utf-8')

    def write(self, relative, value):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return p

    def image(self, aid, source, case='product-x', kind='original', checkpoint=None, **extra):
        path = self.root / 'pictures' / (aid + '.png')
        path.parent.mkdir(exist_ok=True)
        # Different colors make hashes and source identities independent of naming/order.
        Image.new('RGB', (16, 40), (len(self.m['artifacts'])*23 % 255, 50, 90)).save(path)
        a = dict(artifact_id=aid, source_keys=[source], case_id=case, kind=kind,
                 checkpoint=checkpoint, path=path.relative_to(self.root).as_posix(), sha256=digest(path), width=16, height=40,
                 parent_artifact_id=None, **extra)
        self.m['artifacts'].append(a)
        if source not in self.m['source_keys']:
            self.m['source_keys'].append(source)
        self.m['case_inputs'][case] = {'product_name': '임의 상품 ' + case}
        return a

    def finish(self, **kwargs):
        self.write('manifest.json', self.m)
        result = generate(self.root, self.output, **kwargs)
        html = (self.output / 'index.html').read_text(encoding='utf-8')
        data = json.loads(html.split('<script id="viewer-data" type="application/json">')[1].split('</script>')[0])
        return result, data

    def test_dynamic_products_counts_and_portable_parent(self):
        for case, count in [('arbitrary-long-case', 2), ('another', 5)]:
            for i in range(count):
                key = f'{case}/photo-{i}'
                self.image(f'{case}-{i}-original', key, case)
                parent = self.image(f'{case}-{i}-section', key, case, kind='section_image')
                p = self.image(f'{case}-{i}-preview', key, case, kind='preview_section', checkpoint='machine_snapshot')
                p['parent_artifact_id'] = parent['artifact_id']
                self.image(f'{case}-{i}-final', key, case, kind='final_image', checkpoint='service_final')
        report, data = self.finish()
        self.assertEqual(report['source_count'], 7)
        self.assertEqual(report['case_count'], 2)
        self.assertEqual(report['status_counts'], {'generated': 7})
        self.assertEqual(report['warnings'], [])
        self.assertTrue(data['cases'][0]['sources'][0]['sections'][0]['original']['url'])

    def test_db_join_uses_hash_not_source_name_or_upload_order(self):
        a = self.image('source-record', 'non-numeric-source')
        p = self.image('preview', 'non-numeric-source', kind='preview_section', checkpoint='machine_snapshot', target_id=91)
        section = self.image('section-bytes', 'non-numeric-source', kind='section_image')
        raw = (self.root / section['path']).read_bytes()
        objpath = self.root / 'objects' / section['sha256'];objpath.parent.mkdir();objpath.write_bytes(raw)
        self.write('object-index.json', {'opaque-key': {'sha256': section['sha256']}})
        self.write('evidence/product-x.json', {'job': [{'id': 45, 'status': 'done'}],
            'source_image': [{'id': 700, 'upload_order': 999, 'sha256': a['sha256']}],
            'section': [{'id': 91, 'source_image_id': 700, 'section_order': 3, 'top_offset': 42, 'height': 40, 'bucket': 'include', 'image_key': 'opaque-key'}]})
        report, data = self.finish()
        sec = data['cases'][0]['sources'][0]['sections'][0]
        self.assertEqual(sec['id'], '91')
        self.assertEqual(sec['offset'], 42)
        self.assertEqual(sec['previews'][0]['artifact_id'], p['artifact_id'])
        self.assertEqual(report['section_count'], 1)

    def test_failed_job_is_shared_and_not_quality_defects(self):
        for i in range(3):self.image('orig'+str(i), 'key'+str(i))
        self.write('evidence/product-x.json', {'job': [{'id': 8, 'status': 'failed', 'current_step': 'N2'}],
            'job_async_task': [{'id': 22, 'stage': 'analyze', 'attempt_no': 1, 'error_code': 'ANALYZE_UNEXPECTED', 'error_message': '504'}]})
        report, data = self.finish()
        self.assertEqual(report['status_counts'], {'failed': 3})
        self.assertEqual(len(data['cases'][0]['errors']), 1)
        self.assertFalse(report['quality_evaluated'])

    def test_all_excluded_requires_explicit_section_evidence(self):
        a = self.image('original', 'photo')
        self.write('evidence/product-x.json', {'job': [{'status': 'done'}], 'source_image': [{'id': 5, 'sha256': a['sha256']}],
            'section': [{'id': 6, 'source_image_id': 5, 'section_order': 1, 'bucket': 'exclude', 'exclusion_reason': 'auto_local'}]})
        report, data = self.finish()
        self.assertEqual(report['status_counts'], {'excluded': 1})
        self.assertEqual(data['cases'][0]['sources'][0]['finals'], [])
        self.assertFalse(data['reference_frozen'])

    def test_missing_final_is_not_replaced_by_original(self):
        self.image('original', 'photo')
        a = self.image('final', 'photo', kind='final_image', checkpoint='service_final')
        (self.root / a['path']).unlink()
        report, data = self.finish()
        self.assertEqual(report['status_counts'], {'missing': 1})
        self.assertIsNone(data['cases'][0]['sources'][0]['finals'][0]['url'])

    def test_no_output_alone_is_not_entire_exclusion(self):
        self.image('original', 'photo')
        self.root.joinpath('execution.jsonl').write_text(json.dumps({'case_id': 'product-x', 'source_keys': ['photo'], 'result_only': True, 'execution_status': 'no_output'}))
        report, _ = self.finish()
        self.assertEqual(report['status_counts'], {'no_output': 1})

    def test_missing_section_relation_does_not_guess(self):
        self.image('original', 'photo')
        self.image('preview', 'photo', checkpoint='machine_snapshot', kind='preview_section')
        _, data = self.finish()
        self.assertIsNone(data['cases'][0]['sources'][0]['sections'][0]['original']['url'])
        self.assertTrue(data['warnings'])

    def test_hash_mismatch_aborts_without_output(self):
        a = self.image('original', 'photo');a['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'SHA-256'):self.finish()
        self.assertFalse(self.output.exists())

    def test_image_dimension_mismatch(self):
        self.image('original', 'photo')['width'] = 17
        with self.assertRaisesRegex(ValueError, '크기'):self.finish()

    def test_archive_restore_preserves_inputs(self):
        a = self.image('original', 'photo');p=self.root/a['path']
        zpath=self.base/'release.zip'
        with zipfile.ZipFile(zpath,'w') as z:z.write(p,self.m['evaluation_run_id']+'/'+a['path'])
        p.unlink()
        report,data=self.finish(archives=[(zpath,digest(zpath))])
        self.assertTrue(data['cases'][0]['sources'][0]['original']['url'])
        self.assertFalse(p.exists())
        self.assertIn('::',report['inputs'][a['path']]['origin'])

    def test_asset_root_restore(self):
        a=self.image('original','photo');other=self.base/'restored';(other/'pictures').mkdir(parents=True)
        (other/a['path']).write_bytes((self.root/a['path']).read_bytes());(self.root/a['path']).unlink()
        _,data=self.finish(asset_roots=[other])
        self.assertTrue(data['cases'][0]['sources'][0]['original']['url'])

    def test_archive_hash_and_traversal_rejected(self):
        self.image('original','photo');self.write('manifest.json',self.m);zpath=self.base/'bad.zip'
        with zipfile.ZipFile(zpath,'w') as z:z.writestr('../escape','bad')
        with self.assertRaisesRegex(ValueError,'ZIP SHA'):generate(self.root,self.output,archives=[(zpath,'0'*64)])
        with self.assertRaisesRegex(ValueError,'상대 경로'):generate(self.root,self.output,archives=[(zpath,digest(zpath))])
        self.assertFalse(self.output.exists())

    def test_file_path_escape_rejected(self):
        self.image('original','photo')['path']='../outside.png'
        with self.assertRaisesRegex(ValueError,'상대 경로'):self.finish()

    def test_input_and_existing_output_protected(self):
        self.image('original','photo');self.write('manifest.json',self.m)
        with self.assertRaisesRegex(ValueError,'겹치지'):generate(self.root,self.root/'view')
        self.output.mkdir();(self.output/'user.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError,'덮어쓰지'):generate(self.root,self.output)
        self.assertEqual((self.output/'user.txt').read_text(),'keep')

    def test_all_input_files_unchanged(self):
        self.image('original','photo');self.write('manifest.json',self.m)
        before={p.relative_to(self.root):digest(p) for p in self.root.rglob('*') if p.is_file()}
        generate(self.root,self.output)
        self.assertEqual(before,{p.relative_to(self.root):digest(p) for p in self.root.rglob('*') if p.is_file()})

    def test_embedded_data_cannot_close_script(self):
        self.image('original','photo');attack='</script><img src=x onerror=alert(1)>'
        self.m['case_inputs']['product-x']['product_name']=attack
        _,data=self.finish();html=(self.output/'index.html').read_text(encoding='utf-8')
        self.assertNotIn(attack,html)
        self.assertEqual(data['cases'][0]['name'],attack)

    def test_multiple_finals_kept(self):
        self.image('original','photo')
        for i in range(2):self.image('final'+str(i),'photo',kind='final_image',checkpoint='service_final')
        report,data=self.finish()
        self.assertEqual(report['final_images'],2)
        self.assertEqual(len(data['cases'][0]['sources'][0]['finals']),2)

    def test_unrepresented_manifest_source_rejected(self):
        self.m['source_keys']=['not-in-evidence']
        with self.assertRaisesRegex(ValueError,'대상 목록'):self.finish()

    def test_ambiguous_identical_source_hash_is_not_joined(self):
        a=self.image('one','photo1');b=self.image('two','photo2')
        (self.root/b['path']).write_bytes((self.root/a['path']).read_bytes());b['sha256']=a['sha256']
        self.write('evidence/product-x.json',{'job':[{}],'source_image':[{'id':1,'sha256':a['sha256']}],
            'section':[{'id':4,'source_image_id':1,'section_order':1,'bucket':'exclude'}]})
        report,_=self.finish()
        self.assertEqual(report['status_counts'],{'unknown':2})
        self.assertEqual(report['section_count'],0)

    def test_cross_product_source_identity_rejected(self):
        self.image('one','photo',case='a');self.image('two','photo',case='b')
        with self.assertRaisesRegex(ValueError,'다른 상품'):self.finish()

    def test_conflicting_execution_evidence_hash_rejected(self):
        a=self.image('one','photo')
        self.write('execution.jsonl',{'case_id':'product-x','evidence_refs':[{'path':a['path'],'sha256':'0'*64}]})
        with self.assertRaisesRegex(ValueError,'해시 충돌'):self.finish()

    def test_non_json_execution_evidence_not_parsed_as_database(self):
        a=self.image('one','photo')
        self.write('execution.jsonl',{'case_id':'product-x','evidence_refs':[{'path':a['path'],'sha256':a['sha256']}]})
        report,_=self.finish()
        self.assertEqual(report['source_count'],1)


if __name__=='__main__':
    unittest.main()
