"""프로젝트 자동 분류와 재생성 검증. 실제 메일/LLM 불필요."""
import json
import re
import tempfile
import unittest
from datetime import timedelta
from html import unescape
from pathlib import Path
from nwmail.dashboard import build_model, project_progress, deadline_info, render_html
from nwmail.store import Store
from nwmail.feedback import FeedbackStore
from nwmail.tasklinks import task_id
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

    def test_remaining_projects_first_across_clients_and_own_only_tasks(self):
        """최신/기본/고객사 순위보다 실제 잔여 업무가 우선한다."""
        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp)/'mail.db') as st, FeedbackStore(Path(tmp)/'feedback.db') as fb:
                entries = [
                    ('<empty@progress>', '[루미] 참고 공지', '2026-09-11T10:00:00+09:00'),
                    ('<pending@progress>', '오로라 포럼 안내', '2026-09-11T11:00:00+09:00'),
                    ('<mine@progress>', '[KX볼트링크] 화면 검토', '2026-09-10T09:00:00+09:00'),
                    ('<task@progress>', '[하늘투어] 디자인 검토', '2026-09-10T08:00:00+09:00'),
                ]
                st.save_mails([mail(uid, subject, 'sender@example.com', key, to=[ME], when=when)
                               for uid, (key, subject, when) in enumerate(entries, 1)], me=ME)
                put_extraction(st, entries[0][0], {'summary': '참고 공지', 'tasks': [], 'my_actions': []})
                put_extraction(st, entries[2][0], {
                    'summary': '내 검토만 필요', 'tasks': [],
                    'my_actions': [{'text': '화면 시안을 검토하고 회신', 'deadline': TODAY.isoformat()}],
                })
                put_extraction(st, entries[3][0], {
                    'summary': '협력사 디자인 진행', 'my_actions': [],
                    'tasks': [{'text': '여행 페이지 시안 제작', 'role': '디자인', 'assignee': '협력사 담당자'}],
                })
                m = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
                projects = {p['name']: p for p in m['projects']}
                own_only = projects['KX볼트링크']
                self.assertEqual(own_only['open_count'], 0)
                self.assertEqual(own_only['progress']['open'], 1)
                self.assertEqual(own_only['pstate'], 'open')
                active = [p['name'] for p in m['projects'] if p['progress']['open']]
                other = [p['name'] for p in m['projects'] if not p['progress']['open']]
                self.assertEqual(active, ['KX볼트링크', '하늘투어'])
                self.assertEqual([p['name'] for p in m['projects']], active + other)
                self.assertEqual(projects['오로라 포럼']['pstate'], 'open')
                self.assertEqual(projects['오로라 스토어']['pstate'], 'done')
                # 고객사별 렌더링이 모델의 전역 잔여 우선순서를 뒤집지 않아야 한다.
                markup = render_html(m)
                rendered = [unescape(name) for name in re.findall(
                    r'<details class="proj" data-project="([^"]+)"', markup)]
                self.assertEqual(set(rendered), set(projects))
                self.assertLess(max(rendered.index(name) for name in active),
                                min(rendered.index(name) for name in other))

    def test_not_mine_hides_own_tasks_and_undo_preserves_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp)/'mail.db') as st, FeedbackStore(Path(tmp)/'feedback.db') as fb:
                st.save_mails([mail(1, '[루미] 기획안', 'sender@example.com', KEY, to=[ME])], me=ME)
                data = payload()
                completed_text = '기존 완료 업무 기록'
                data['my_actions'].append({'text': completed_text, 'deadline': TODAY.isoformat()})
                completed_id = task_id(KEY, 'me', completed_text)
                put_extraction(st, KEY, data)
                fb.apply(completed_id, 'done', snapshot={'text': completed_text, 'kind': 'me'}, now=NOW)
                fb.set_due(CANON, TODAY.isoformat(), now=NOW)

                def model():
                    return build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)

                initial = model()
                project = initial['projects'][0]
                states, dues = fb.states(), fb.dues()
                self.assertEqual(initial['stats']['my_open'], 1)
                self.assertEqual(initial['stats']['sched_badge'], 1)
                fb.mark('project', project['name'], 'not_mine', project['received_at'], now=NOW)
                hidden = model()
                self.assertEqual(hidden['projects'][0]['pstate'], 'not_mine')
                self.assertEqual(hidden['stats']['projects_not_mine'], 1)
                self.assertEqual(hidden['stats']['my_open'], 0)
                self.assertEqual(hidden['stats']['due_today'], 0)
                self.assertEqual(hidden['stats']['sched_badge'], 0)
                self.assertEqual(hidden['my_actions'], [])
                self.assertEqual([a['id'] for a in hidden['hidden_actions']], [CANON])
                self.assertEqual([a['id'] for a in hidden['done_actions']], [completed_id])
                self.assertEqual(fb.states(), states)
                self.assertEqual(fb.dues(), dues)
                # 기존 기한/업무를 DOM에 보관하여 새로고침 후에도 되돌리기가 가능하다.
                self.assertIn(CANON, {a['id'] for a in hidden['schedule']})
                self.assertIn(f'<template id="cv-{CANON}">', render_html(hidden))

                fb.mark('project', project['name'], 'open', now=NOW + timedelta(seconds=1))
                restored = model()
                self.assertEqual(restored['stats']['my_open'], initial['stats']['my_open'])
                self.assertEqual(restored['stats']['sched_badge'], initial['stats']['sched_badge'])
                self.assertEqual([a['id'] for a in restored['my_actions']], [CANON])
                self.assertEqual(restored['hidden_actions'], [])
                self.assertEqual(fb.states(), states)
                self.assertEqual(fb.dues(), dues)
                self.assertEqual([r['action'] for r in fb.conn.execute(
                    "SELECT action FROM mark_log WHERE target='project' ORDER BY id")], ['not_mine', 'open'])

    def test_not_mine_persists_after_new_received_mail_and_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp)/'mail.db') as st, FeedbackStore(Path(tmp)/'feedback.db') as fb:
                st.save_mails([mail(1, '[루미] 기획안', 'sender@example.com', KEY, to=[ME])], me=ME)
                put_extraction(st, KEY, payload())
                before = build_model(st, me=ME, today=TODAY, now=NOW, feedback=fb)
                project = before['projects'][0]
                fb.mark('project', project['name'], 'not_mine', project['received_at'], now=NOW)
                later = NOW + timedelta(hours=1)
                st.save_mails([mail(2, 'RE: [루미] 기획안', 'sender@example.com', '<reply@progress>',
                                    to=[ME], refs=[KEY], when=later.isoformat())], me=ME)
                # 새 메일을 반영한 새 업무도 프로젝트 상태를 명시적으로 되돌리기 전에는 숨긴다.
                data = payload()
                data['my_actions'].append({'text': '새 요청사항 검토', 'deadline': TODAY.isoformat()})
                st.conn.execute('UPDATE extractions SET payload=? WHERE thread_key=?', (json.dumps(data), KEY))
                st.conn.commit()
                after = build_model(st, me=ME, today=TODAY, now=later, feedback=fb)
                self.assertGreater(after['projects'][0]['received_at'], project['received_at'])
                self.assertEqual(after['projects'][0]['pstate'], 'not_mine')
                self.assertEqual(after['stats']['my_open'], 0)
                self.assertEqual(len(after['hidden_actions']), 2)
                self.assertEqual(fb.marks('project')[project['name']]['label'], 'not_mine')


if __name__=='__main__': unittest.main()
