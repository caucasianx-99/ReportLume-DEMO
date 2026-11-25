from datetime import date
from pathlib import Path
import re
import uuid

from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
from app.core.local_store import database, guest_for


def test_free_workspace_flow(tmp_path, monkeypatch):
    monkeypatch.setenv('REPORTLUME_DB', str(tmp_path / 'workspace.sqlite3'))
    a, b = TestClient(app), TestClient(app)
    assert a.get('/login').status_code == 404
    assert a.get('/signup').status_code == 404
    assert a.get('/start').status_code == 200
    csrf = a.cookies[settings.csrf_cookie_name]
    assert a.post('/start', data={'csrf_token': 'wrong'}).status_code == 403
    assert a.post('/start', data={'csrf_token': csrf}, follow_redirects=False).status_code == 303
    token = a.cookies[settings.session_cookie_name]
    assert a.get('/dashboard').status_code == 200
    cid = uuid.uuid4().hex
    r = a.post('/clients', data={'csrf_token': csrf, 'client_id': cid, 'display_name': 'Fictional test', 'x_handle': 'example_writer', 'collaboration_started': '2026-10-01', 'authorization': 'on'}, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers['location']
    assert a.get(url).status_code == 200
    assert b.get(url, follow_redirects=False).headers['location'] == '/start'
    raw = (Path(__file__).parent / 'fixtures/x_content_export.csv').read_bytes()
    headers = {'content-type': 'text/csv', 'x-csrf-token': csrf, 'x-upload-filename': 'test.csv'}
    api = '/api' + url + '/imports'
    r = a.post(api + '/preview', content=raw, headers=headers)
    assert r.status_code == 200
    preview = r.json(); start = preview['published_start']; end = preview['published_end']
    headers.update({'x-preview-token': preview['preview_token'], 'x-confirm-import': 'yes', 'x-export-start': start, 'x-export-end': end, 'x-measurement-date': max(end, date.today().isoformat()), 'x-measurement-zone': 'UTC'})
    r = a.post(api + '/confirm', content=raw, headers=headers)
    assert r.status_code == 200 and r.json()['rows'] == 10
    assert not r.json()['trial_started']
    rp = url + '/reports/' + start[:7]
    assert a.get(rp).status_code == 200
    r = a.post(rp + '/action', data={'csrf_token': csrf, 'action': 'new_revision', 'brand': 'Fictional test'}, follow_redirects=False)
    assert r.status_code == 303
    rev = r.headers['location'].split('revision=')[1]
    assert a.get(rp + '/revisions/' + rev).status_code == 200
    r = a.post(rp + '/action', data={'csrf_token': csrf, 'action': 'publish', 'revision': rev, 'confirm_report_review': 'on', 'confirm_incomplete_month': 'on'}, follow_redirects=False)
    assert r.status_code == 303
    r = a.post(rp + '/action', data={'csrf_token': csrf, 'action': 'new_share', 'role': 'viewer'})
    assert r.status_code == 200
    share = re.search(r'/r/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', r.text).group()
    assert b.get(share).status_code == 200
    b.get('/start'); csrf2 = b.cookies[settings.csrf_cookie_name]
    assert b.post('/start', data={'csrf_token': csrf2}, follow_redirects=False).status_code == 303
    assert b.get(url).status_code == 404
    assert a.post('/workspace/reset', data={'csrf_token': csrf}).status_code == 400
    assert a.post('/workspace/reset', data={'csrf_token': csrf, 'confirm_reset': 'yes'}, follow_redirects=False).status_code == 303
    assert guest_for(token) is None
    assert b.get(share).status_code == 404
    uid = guest_for(b.cookies[settings.session_cookie_name])
    with database() as db:
        db.execute('UPDATE guests SET expires=0 WHERE uid=?', (uid,))
    assert b.get('/dashboard', follow_redirects=False).status_code == 303
