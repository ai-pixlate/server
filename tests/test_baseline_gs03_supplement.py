"""실제 모델·DB 없이 보완 실험 경계를 검증한다."""
import ast
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from tools.baseline import gs03_supplement as s, gs03_runner as runner, be
from tools.baseline.core import write_new

class SupplementTest(unittest.TestCase):
    def test_only_merge_timeout_changes(self):
        raw='[merge]\nllm_timeout_s = 60 # same\nllm_model="gemini-3.8-flash"\n[judge]\nllm_timeout_s = 60\n'
        self.assertEqual(s.override_config(raw),raw.replace('= 60 #','= 120 #'))
        with self.assertRaises(ValueError):s.override_config(raw.replace('= 60 #','= 90 #'))
    def test_scope_and_unapproved_execution(self):
        self.assertEqual(len(s.SOURCES),18)
        self.assertTrue(all(x.startswith('GS-03') for x in s.SOURCES))
        with tempfile.TemporaryDirectory() as tmp:
            write_new(Path(tmp)/'manifest.json',{'cost_approval':{'user_execution':None}})
            with patch.object(runner,'check_runtime') as check:
                with self.assertRaisesRegex(ValueError,'승인 보류'):runner._run_prepared(tmp,True)
                check.assert_not_called()
            with self.assertRaises(ValueError):runner._run_prepared(tmp,False)
    def test_runner_selects_only_gs03(self):
        tree=ast.parse(Path(runner.__file__).read_text(encoding='utf-8'))
        cases=[ast.literal_eval(n.iter) for n in ast.walk(tree) if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='case']
        self.assertEqual(cases,[('GS-03',)])
    def test_budget_stops_before_fake_call(self):
        for changes in [{'max_calls':1},{'max_amount':1.04}]:
            b={**s.BUDGET,**changes};f=Mock(return_value=object());meter=be.Meter(b,lambda e:None)
            meter.call(f,model=b['model'])
            with self.assertRaises(be.BudgetStop):meter.call(f,model=b['model'])
            self.assertEqual(f.call_count,1)
    def test_failed_call_reservation_remains(self):
        b={**s.BUDGET,'max_amount':1.04};meter=be.Meter(b,lambda e:None);f=Mock(side_effect=RuntimeError('synthetic'))
        with self.assertRaises(RuntimeError):meter.call(f,model=b['model'])
        with self.assertRaises(be.BudgetStop):meter.call(f,model=b['model'])
        self.assertEqual(f.call_count,1)
    def test_local_storage_immutable_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=be.FileStore(Path(tmp)/'objects');store.put('x',b'original');sha=store.index['x']['sha256']
            store.put('x',b'new');self.assertEqual((store.root/sha).read_bytes(),b'original')
    def test_canonical_reference_order(self):
        self.assertEqual(s.canonical({'rows':[{'id':2},{'id':1}]}),s.canonical({'rows':[{'id':1},{'id':2}]}))

class PreparationChainTest(unittest.TestCase):
    def setUp(self):
        import json
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.baseline=self.root/'baseline';self.product=self.root/'product';self.out=self.root/'supplement'
        (self.baseline/'artifacts').mkdir(parents=True);(self.product/'pipeline/config').mkdir(parents=True)
        self.config=b'[merge]\nllm_timeout_s = 60\n'
        (self.product/'pipeline/config/default.toml').write_bytes(self.config)
        m=s.protocol.template();m.update(product_commit=s.TARGET,product_version='1.0.1',artifacts=[],
            environment={'model_assets':[],'dependency_lock':{'path':'lock','sha256':''}},
            generation_config={},case_inputs={},dataset_manifest_path='artifacts/dataset-manifest.json')
        (self.baseline/'lock').write_text('synthetic lock')
        write_new(self.baseline/'references.json',{'expression_dictionary':[{'id':1}]})
        write_new(self.baseline/m['dataset_manifest_path'],{'files':[{'source_id':x} for x in s.IDS]})
        m['dataset_manifest_sha256']=s.digest(self.baseline/m['dataset_manifest_path'])
        m['measurement_scope']={'measured_quality_metrics':['H01','H02','H03','H04'],'human_intervention':'confirmation_only'}
        for sid in s.GS03_IDS:
            path='artifacts/'+sid+'.jpg';(self.baseline/path).write_bytes(sid.encode())
            m['artifacts'].append({'width':1,'height':1,'kind':'original','case_id':'GS-03','artifact_id':sid,'path':path,'sha256':s.digest(self.baseline/path)})
        write_new(self.baseline/'manifest.json',m)
        before=s.tree_hashes(self.baseline)
        def git(args,**kw):
            if 'rev-parse' in args:return s.TARGET+'\n'
            if 'ls-files' in args:return 'pipeline/config/default.toml\n'
            return self.config
        with patch.object(s.subprocess,'check_output',side_effect=git):s.prepare(self.baseline,self.product,self.out)
        self.assertEqual(s.tree_hashes(self.baseline),before)
    def test_prepare_both_validators_and_preflight(self):
        self.assertEqual(s.validate(self.out),[])
        self.assertEqual(s.protocol.validate(self.out),[])
        self.assertEqual((self.out/'artifacts/dataset-manifest.json').read_bytes(),(self.baseline/'artifacts/dataset-manifest.json').read_bytes())
        with patch('tools.baseline.preflight.verify_dependency_lock'),patch.object(be,'check_runtime',return_value=(self.product,'mock-url')),patch.object(s,'database_preflight',return_value={'read_only':True}) as db:
            self.assertEqual(s.preflight(self.out),{'read_only':True});db.assert_called_once_with(self.out,'mock-url')
        self.assertFalse((self.out/'started.json').exists())
    def test_runner_reaches_native_boundary_only_after_preflight(self):
        import subprocess
        import sys
        import textwrap
        code = textwrap.dedent("""
            import sys
            from pathlib import Path
            from unittest.mock import patch
            sys.path.insert(0, sys.argv[1])
            from tools.baseline import gs03_supplement as s, gs03_runner as runner, be
            out, product = map(Path, sys.argv[2:4])
            with patch.object(s,'require_approval'), patch('tools.baseline.preflight.verify_dependency_lock'), patch.object(be,'check_runtime',return_value=(product,'mock-url')), patch.object(runner,'check_runtime',return_value=(product,'mock-url')), patch.object(s,'database_preflight',return_value={'read_only':True}) as db, patch.object(runner,'initialize_native_runtime',side_effect=AssertionError('native initialization forbidden')) as native:
                try:
                    runner._run_prepared(out,True)
                except ValueError as error:
                    assert '장비' in str(error), str(error)
                else:
                    raise AssertionError('synthetic incomplete environment must stop')
                db.assert_called_once()
                native.assert_not_called()
            assert not (out/'started.json').exists()
        """)
        repo=Path(__file__).resolve().parents[1]
        # 전체 pytest 수집이 app/pipeline을 먼저 import해도 자식 프로세스는 깨끗하다.
        with patch.dict(sys.modules, {'pipeline.test_contamination':Mock()}):
            result=subprocess.run([sys.executable,'-I','-B','-c',code,str(repo),str(self.out),str(self.product)],
                                  capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.out/'started.json').exists())

    def test_missing_dataset_stops_before_database(self):
        (self.out/'artifacts/dataset-manifest.json').unlink()
        self.assertTrue(s.protocol.validate(self.out));self.assertTrue(s.validate(self.out))
        with patch.object(s,'database_preflight') as db:
            with self.assertRaises(ValueError):s.preflight(self.out)
            db.assert_not_called()
    def test_runner_preflight_failures_do_not_claim_start(self):
        for error in (ConnectionError('connection'),RuntimeError('schema'),ValueError('dictionary')):
            with self.subTest(error=type(error).__name__),patch.object(s,'require_approval'),patch.object(runner,'preflight',side_effect=error),patch.object(runner,'initialize_native_runtime') as native:
                with self.assertRaises(type(error)):runner._run_prepared(self.out,True)
                native.assert_not_called()
                self.assertFalse((self.out/'started.json').exists())
                self.assertFalse((self.out/'generation-claims').exists())
    def test_existing_start_is_not_removed(self):
        marker=self.out/'started.json';marker.write_bytes(b'existing')
        with patch.object(s,'database_preflight') as db:
            with self.assertRaisesRegex(ValueError,'재실행'):s.preflight(self.out)
            db.assert_not_called()
        self.assertEqual(marker.read_bytes(),b'existing')
    def test_database_read_only_schema_and_reference_failures(self):
        tables=('alembic_version','master_country','master_language','category_master','category_mapping','expression_dictionary','expression_dictionary_evidence','glossary','master_data_version')
        from datetime import date, datetime, timezone
        actual={t:[{'id':1}] for t in tables}
        actual['expression_dictionary']=[{'id':1,'confirmed_date':date(2026,9,16),'optional_date':None}]
        actual['glossary']=[{'id':1,'verified_at':date(2026,9,18),'loaded_at':datetime(2026,9,18,12,30,tzinfo=timezone.utc)}]
        # PostgreSQL DATE/TIMESTAMP가 반환하는 Python 자료형을 실제 저장 형식과 비교.
        import json
        expected=json.loads(json.dumps(actual,default=str))
        # Synthetic expectation only; baseline input remains untouched.
        import json
        (self.out/'expected-references.json').write_text(json.dumps(expected))
        url='postgresql+psycopg://localhost/px_baseline_gs03_supplement_20261126'
        for mode in ('ok','connection','schema','dictionary','date_mismatch'):
            with self.subTest(mode=mode):
                engine=Mock();conn=Mock();engine.connect.return_value.__enter__=Mock(return_value=conn);engine.connect.return_value.__exit__=Mock(return_value=False)
                queries=[]
                def execute(stmt):
                    sql=str(stmt);queries.append(sql);result=Mock();result.scalar_one.return_value=0
                    if mode=='schema' and 'FROM alembic_version' in sql:raise RuntimeError('missing schema')
                    table=sql.rsplit(' ',1)[-1];rows=actual.get(table,[])
                    if mode=='dictionary' and table=='glossary':rows=[{'id':2}]
                    if mode=='date_mismatch' and table=='expression_dictionary':
                        rows=[{**actual[table][0],'confirmed_date':date(2026,9,17)}]
                    result.mappings.return_value=rows;return result
                conn.execute.side_effect=execute
                if mode=='connection':engine.connect.side_effect=ConnectionError('connection')
                with patch('sqlalchemy.create_engine',return_value=engine):
                    if mode=='ok':self.assertTrue(s.database_preflight(self.out,url)['read_only'])
                    else:
                        with self.assertRaises((ValueError,RuntimeError,ConnectionError)):s.database_preflight(self.out,url)
                engine.dispose.assert_called_once()
                if queries:self.assertEqual(queries[0],'SET TRANSACTION READ ONLY')
                self.assertTrue(all(q.startswith(('SET TRANSACTION READ ONLY','SELECT ')) for q in queries))
                self.assertFalse((self.out/'started.json').exists())

if __name__=='__main__':unittest.main()
