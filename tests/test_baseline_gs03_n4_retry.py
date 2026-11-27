import copy
import unittest
from tools.baseline.gs03_n4_retry import retry_candidates


class N4RetryTest(unittest.TestCase):
    def data(self):
        return {'job':[{'current_step':'N4','status':'failed'}],
                'job_async_task':[
                    {'id':41,'stage':None,'status':'running','cancelled_at':None},
                    {'id':227,'stage':'translate','is_current':True,'status':'failed',
                     'parent_task_id':41,'unit_id':27,'error_code':'TRANSLATE_ALL_FAILED',
                     'retry_count':1,'max_retry':2,'input_manifest':{'expected':[232]}},
                    {'id':228,'stage':'translate','is_current':True,'status':'failed',
                     'parent_task_id':41,'unit_id':4,'error_code':'TRANSLATE_PARTIAL',
                     'retry_count':1,'max_retry':2,'input_manifest':{'expected':[56,57,58]}}],
                'text_block':[{'id':i,'block_status':'machine','trans_1':v}
                              for i,v in [(232,None),(56,None),(57,None),(58,'kept')]]}

    def test_blocker_first_only_failed_blocks_and_no_mutation(self):
        d=self.data();before=copy.deepcopy(d);rows=retry_candidates(d)
        self.assertEqual(rows[0]['task_id'],227)
        self.assertEqual(rows[0]['failed_block_ids'],[232])
        self.assertEqual(rows[0]['retries_remaining'],1)
        self.assertEqual(rows[1]['failed_block_ids'],[56,57])
        self.assertEqual(rows[1]['preserved_block_ids'],[58])
        self.assertFalse(rows[1]['blocking']);self.assertEqual(d,before)

    def test_exhausted_limit_is_not_reset(self):
        d=self.data();d['job_async_task'][1]['retry_count']=2
        self.assertEqual(retry_candidates(d)[0]['retries_remaining'],0)

    def test_cancelled_or_completed_run_rejected(self):
        for key,value in [('status','done'),('cancelled_at','2026-11-27')]:
            d=self.data();d['job_async_task'][0][key]=value
            with self.assertRaises(ValueError):retry_candidates(d)

    def test_nonfailed_job_rejected(self):
        d=self.data();d['job'][0]['status']='processing'
        with self.assertRaises(ValueError):retry_candidates(d)

    def test_old_attempt_and_success_are_not_retried(self):
        d=self.data();old=copy.deepcopy(d['job_async_task'][1]);old.update(id=163,is_current=False)
        d['job_async_task'].append(old)
        self.assertEqual(len(retry_candidates(d)),2)

    def test_user_edited_block_is_not_targeted(self):
        d=self.data();d['text_block'][1]['block_status']='edited'
        self.assertEqual(retry_candidates(d)[1]['failed_block_ids'],[57])


if __name__=='__main__':unittest.main()
