"""DB 생성 안전 경계만 모의 검증. 실제 접속·모델 호출 없음."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch,MagicMock
from tools.baseline import gs03_database as d
from tools.baseline.core import write_new

class DatabasePreparationTest(unittest.TestCase):
    def test_reject_other_database_and_remote_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'connection.txt'
            for url in ('postgresql+psycopg://localhost/px_baseline_original',
                        'postgresql+psycopg://example.com/'+d.DB_NAME):
                p.write_text(url)
                with self.assertRaises(ValueError):d.database_url(p)
    def test_existing_database_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);u=p/'connection.txt';u.write_text('postgresql+psycopg://localhost/'+d.DB_NAME)
            write_new(p/'manifest.json',{'generation_config':{'product_root':tmp},'environment':{'dependency_lock':{'path':'lock'}}})
            write_new(p/'expected-references.json',{'alembic_version':[{'version_num':'0008'}]})
            engine=MagicMock();conn=engine.connect.return_value.__enter__.return_value
            conn.execute.return_value.scalar.return_value=1
            with patch.object(d,'validate',return_value=[]),patch('tools.baseline.preflight.verify_dependency_lock'),patch.object(d.subprocess,'check_output',return_value=d.TARGET),patch('sqlalchemy.create_engine',return_value=engine),patch.object(d.subprocess,'run') as migrate:
                with self.assertRaisesRegex(ValueError,'덮어쓰지'):d.restore(p,u)
                migrate.assert_not_called()
                self.assertEqual(conn.execute.call_count,1)
                self.assertTrue(str(conn.execute.call_args.args[0]).startswith('SELECT '))
                engine.dispose.assert_called_once()
if __name__=='__main__':unittest.main()
