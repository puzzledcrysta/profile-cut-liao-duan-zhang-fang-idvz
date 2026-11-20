import json
from app.errors import DataError
from app.store import connection


def lookup(db, correction_id):
    row = db.execute('SELECT * FROM corrections WHERE id=?', (correction_id,)).fetchone()
    if row is None:
        raise DataError('CORRECTION_UNKNOWN', '没有此补正记录', 404)
    return row


def fetch(db, correction_id):
    return lookup(db, correction_id)


def signed_runs_for_keys(db, keys):
    """返回 {run_id: set(命中的业务键)}，只含已签核批次。"""
    if not keys:
        return {}
    hits = {}
    run_rows = db.execute("""
        SELECT r.id, r.result FROM runs r
        WHERE EXISTS (SELECT 1 FROM reviews v WHERE v.run_id = r.id)
    """).fetchall()
    for run in run_rows:
        accepted = {row['key'] for row in json.loads(run['result'])['accepted']}
        matched = accepted & set(keys)
        if matched:
            hits[run['id']] = matched
    return hits


def persist(db, correction_id, source, source_hash, status, created, result, impacts):
    db.execute('INSERT INTO corrections VALUES (?,?,?,?,?,?)',
               (correction_id, source, json.dumps(result, ensure_ascii=False),
                source_hash, status, created))
    for item in impacts:
        db.execute(
            'INSERT INTO correction_impacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (correction_id, item['run_id'], item['key'], item['status'],
             item['reason_code'], item['old_revision'], item['new_revision'],
             json.dumps(item['changed_fields'], ensure_ascii=False),
             item['old_amount'], item['new_amount'],
             json.dumps(item['old_raw'], ensure_ascii=False),
             json.dumps(item['new_raw'], ensure_ascii=False),
             json.dumps(item['new_proof'], ensure_ascii=False)))


def load_impacts(db, correction_id):
    rows = db.execute(
        'SELECT * FROM correction_impacts WHERE correction_id=? ORDER BY run_id,key',
        (correction_id,)).fetchall()
    impacts = []
    for row in rows:
        item = dict(row)
        item['changed_fields'] = json.loads(item['changed_fields'])
        item['new_proof'] = json.loads(item['new_proof'])
        item['old_raw'] = json.loads(item['old_raw'])
        item['new_raw'] = json.loads(item['new_raw'])
        impacts.append(item)
    return impacts


def history():
    with connection() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,status,source_sha256,created FROM corrections ORDER BY created DESC,id DESC LIMIT 50')]
