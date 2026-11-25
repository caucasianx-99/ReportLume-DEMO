"""Local SQLite persistence and expiring anonymous browser workspaces."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

TTL = 7 * 86400
MAX_WORKSPACES = 500
MAX_DOCUMENTS = 30000
MAX_BYTES = 64 * 1024 * 1024

@contextmanager
def database():
    path = Path(os.getenv('REPORTLUME_DB', str(Path.home() / '.reportlume' / 'workspace.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=15)
    if os.name != 'nt':
        path.chmod(0o600)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE IF NOT EXISTS guests (uid TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS docs (uid TEXT NOT NULL REFERENCES guests(uid) ON DELETE CASCADE, name TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(uid,name))')
        with db:
            yield db
    finally:
        db.close()

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def guest_for(token):
    if not isinstance(token,str) or len(token)!=43 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in token):
        return None
    with database() as db:
        row=db.execute('SELECT uid FROM guests WHERE token_hash=? AND expires>?',(digest(token),int(time.time()))).fetchone()
        return row[0] if row else None

def new_guest():
    token=secrets.token_urlsafe(32)
    uid=secrets.token_hex(16)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM guests WHERE expires<=?',(int(time.time()),))
        if db.execute('SELECT COUNT(*) FROM guests').fetchone()[0]>=MAX_WORKSPACES:
            raise RuntimeError('Workspace capacity reached')
        db.execute('INSERT INTO guests VALUES (?,?,?)',(uid,digest(token),int(time.time())+TTL))
        name=f'projects/local/databases/(default)/documents/users/{uid}'
        body={'name':name,'fields':{'created_via':{'stringValue':'anonymous_workspace'}},'updateTime':secrets.token_hex(16)}
        db.execute('INSERT INTO docs VALUES (?,?,?)',(uid,name,json.dumps(body)))
    return token

def delete_guest(uid):
    with database() as db:
        db.execute('DELETE FROM guests WHERE uid=?',(uid,))

def execute(store, method, url, **kwargs):
    # Compatibility protocol used by existing report services. No HTTP requests.
    from app.core.firestore_rest import FirestoreUnavailable, FirestoreNotFound, FirestoreConflict, decode_value
    prefix='https://firestore.googleapis.com/v1/'
    if not url.startswith(prefix): raise ValueError('Unexpected storage URL')
    resource=url[len(prefix):]
    params=kwargs.get('params') or {}; payload=kwargs.get('json') or {}
    uid=store.uid
    def value(d, field):
        if field=='__name__': return d['name']
        return decode_value(d['fields'][field]) if field in d.get('fields',{}) else None
    def raw(v):
        return v['referenceValue'] if 'referenceValue' in v else decode_value(v)
    with database() as db:
        db.execute('BEGIN IMMEDIATE' if method=='POST' else 'BEGIN')
        if not db.execute('SELECT 1 FROM guests WHERE uid=? AND expires>?',(uid,int(time.time()))).fetchone():
            raise FirestoreNotFound('Workspace expired or missing')
        def get(name):
            store._check_resource(name)
            row=db.execute('SELECT body FROM docs WHERE uid=? AND name=?',(uid,name)).fetchone()
            return json.loads(row[0]) if row else None
        def descendants(parent):
            store._check_resource(parent)
            p=parent+'/'
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM docs WHERE uid=? AND substr(name,1,?)=?',(uid,len(p),p))]
        def children(parent):
            return [d for d in descendants(parent) if d['name'].count('/')==parent.count('/')+1]
        if method=='GET':
            store._check_resource(resource)
            if 'pageSize' not in params:
                d=get(resource)
                if d is None: raise FirestoreNotFound('Document not found')
                return d
            docs=children(resource)
            order=params.get('orderBy','__name__').split(); field=order[0]
            docs=[d for d in docs if value(d,field) is not None]
            docs.sort(key=lambda d:(value(d,field),d['name']),reverse=len(order)>1 and order[1].lower()=='desc')
            try: offset=int(params.get('pageToken','0'))
            except ValueError: raise FirestoreUnavailable('Invalid page token')
            if offset<0: raise FirestoreUnavailable('Invalid page token')
            size=int(params['pageSize']); result={'documents':docs[offset:offset+size]}
            if offset+size<len(docs): result['nextPageToken']=str(offset+size)
            return result
        if method!='POST': raise ValueError('Unsupported storage operation')
        if resource==store.database+'/documents:batchGet':
            return [{'found':d} if (d:=get(n)) else {'missing':n} for n in payload['documents']]
        if resource==store.base_name+':commit':
            results=[]
            for write in payload['writes']:
                name=write['update']['name'] if 'update' in write else write['delete']; old=get(name)
                cond=write.get('currentDocument',{})
                if ('exists' in cond and cond['exists']!=(old is not None)) or ('updateTime' in cond and (not old or old['updateTime']!=cond['updateTime'])):
                    raise FirestoreConflict('Write conflict')
                if 'delete' in write:
                    db.execute('DELETE FROM docs WHERE uid=? AND name=?',(uid,name)); results.append({}); continue
                fields=write['update']['fields']; mask=write.get('updateMask')
                if mask:
                    merged=dict(old['fields']) if old else {}
                    for key in mask['fieldPaths']:
                        if key in fields: merged[key]=fields[key]
                        else: merged.pop(key,None)
                    fields=merged
                version=secrets.token_hex(16)
                d={'name':name,'fields':fields,'updateTime':version}
                db.execute('INSERT OR REPLACE INTO docs VALUES (?,?,?)',(uid,name,json.dumps(d)))
                results.append({'updateTime':version})
            count,total=db.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM docs WHERE uid=?',(uid,)).fetchone()
            if count>MAX_DOCUMENTS or total>MAX_BYTES: raise FirestoreUnavailable('Workspace storage limit reached')
            return {'writeResults':results}
        if resource.endswith(':listCollectionIds'):
            parent=resource.removesuffix(':listCollectionIds'); docs=descendants(parent)
            ids=sorted({d['name'][len(parent)+1:].split('/')[0] for d in docs})
            offset=int(payload.get('pageToken','0')); size=int(payload.get('pageSize',100)); result={'collectionIds':ids[offset:offset+size]}
            if offset+size<len(ids): result['nextPageToken']=str(offset+size)
            return result
        if resource.endswith(':runQuery'):
            parent=resource.removesuffix(':runQuery'); q=payload['structuredQuery']; src=q['from'][0]
            docs=descendants(parent) if src.get('allDescendants') else children(parent+'/'+src['collectionId'])
            def matches(d,f):
                if 'compositeFilter' in f:
                    c=f['compositeFilter']; assert c['op']=='AND'; return all(matches(d,x) for x in c['filters'])
                ff=f['fieldFilter']; a=value(d,ff['field']['fieldPath']); b=raw(ff['value']); op=ff['op']
                if a is None: return False
                if op=='GREATER_THAN_OR_EQUAL': return a>=b
                if op=='LESS_THAN_OR_EQUAL': return a<=b
                if op=='EQUAL': return a==b
                raise ValueError('Unsupported query filter')
            if 'where' in q: docs=[d for d in docs if matches(d,q['where'])]
            orders=q.get('orderBy',[{'field':{'fieldPath':'__name__'}}]); keys=[o['field']['fieldPath'] for o in orders]
            docs=[d for d in docs if all(value(d,k) is not None for k in keys)]
            for o,k in reversed(list(zip(orders,keys))): docs.sort(key=lambda d:value(d,k),reverse=o.get('direction')=='DESCENDING')
            if 'startAt' in q:
                cursor=tuple(raw(v) for v in q['startAt']['values']); inclusive=q['startAt'].get('before',False)
                def after(d):
                    current=tuple(value(d,k) for k in keys[:len(cursor)])
                    for a,b,o in zip(current,cursor,orders):
                        if a!=b: return a<b if o.get('direction')=='DESCENDING' else a>b
                    return inclusive
                docs=[d for d in docs if after(d)]
            return [{'document':d} for d in docs[:int(q.get('limit',len(docs)))]]
        raise ValueError('Unsupported storage operation')
