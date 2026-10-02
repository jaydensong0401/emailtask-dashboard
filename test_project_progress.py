"""프로젝트 자동 분류와 재생성 검증. 실제 메일/LLM 불필요."""
import json
import tempfile
import unittest
from pathlib import Path
from nwmail.dashboard import build_model, project_progress, deadline_info, render_html
from nwmail.store import Store
from nwmail.feedback import FeedbackStore
from test_dashboard import TODAY, NOW, ME, mail, put_extraction
from test_tasklinks import payload, KEY, CANON


class ProjectProgressTest(unittest.TestCase):
    def item(self, state='open', due='', id='one'):
        return {'id': id, 'state': None if state=='open' else {'label': state},
                'deadline': deadline_info(due, TODAY)}

    def test_empty_extracted_vs_pending(self):
        self.assertEqual(project_progress([], False, None)['state'], 'open')
        self.assertEqual(project_progress([], True, None)['state'], 'done')

    def test_overdue_only_unfinished(self):
        for state in ('done','excluded','deleted'):
            self.assertEqual(project_progress([self.item(state,'2026-09-01')],True,None)['state'],'done')
        self.assertEqual(project_progress([self.item(due='2026-09-01')],True,None)['state'],'delayed')
        self.assertEqual(project_progress([self.item(due=TODAY.isoformat())],True,None)['state'],'open')
        self.assertEqual(project_progress([self.item(due='날짜 미정')],True,None)['state'],'open')

    def test_linked_tasks_count_once_and_pending_prevents_done(self):
        i=self.item('done')
        result=project_progress([i,i],True,None)
        self.assertEqual(result,{'state':'done','total':1,'open':0})
        self.assertEqual(project_progress([i],False,None)['state'],'open')

    def test_manual_deleted_preserved(self):
        self.assertEqual(project_progress([self.item(due='2026-09-01')],True,{'label':'deleted'})['state'],'deleted')

    def test_model_task_undo_deadline_and_new_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp)/'mail.db') as st, FeedbackStore(Path(tmp)/'feedback.db') as fb:
                st.save_mails([mail(1,'[루미] 기획안','sender@example.com',KEY,to=[ME])],me=ME)
                data=payload()
                put_extraction(st,KEY,data)
                def model(): return build_model(st,me=ME,today=TODAY,now=NOW,feedback=fb)
                self.assertEqual(model()['projects'][0]['pstate'],'open')
                fb.set_due(CANON,'2026-09-01',now=NOW)
                m=model()
                self.assertEqual(m['projects'][0]['pstate'],'delayed')
                self.assertEqual(len(m['schedule']),1)
                fb.apply(CANON,'done',now=NOW)
                m=model()
                self.assertEqual(m['projects'][0]['pstate'],'done')
                self.assertFalse(fb.marks('project'))  # 자동 완료는 별도 수동 표시를 남기지 않음
                html=render_html(m)
                self.assertIn('data-progress-view="delayed"',html)
                self.assertNotIn('새 메일이 온 프로젝트부터',html)
                fb.apply(CANON,'open',now=NOW)
                self.assertEqual(model()['projects'][0]['pstate'],'delayed')
                fb.set_due(CANON,'',now=NOW)
                self.assertEqual(model()['projects'][0]['pstate'],'open')
                fb.apply(CANON,'done',now=NOW)
                data['my_actions'].append({'text':'새 요청사항 검토','deadline':''})
                st.conn.execute('UPDATE extractions SET payload=? WHERE thread_key=?',(json.dumps(data),KEY))
                st.conn.commit()
                self.assertEqual(model()['projects'][0]['pstate'],'open')


if __name__=='__main__': unittest.main()
