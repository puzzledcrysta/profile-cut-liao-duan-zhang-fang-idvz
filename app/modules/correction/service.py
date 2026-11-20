import json
from datetime import datetime, timezone
from hashlib import sha256
from app.errors import DataError
from app.settings import FIELDS
from app.store import connection
from app.modules.domain.rule import evaluate
from app.modules.runs.repository import lookup
from app.pipeline import stages
from .repository import fetch, load_impacts, persist, signed_runs_for_keys


def correction_id(source_hash):
    return sha256(f'correction:{source_hash}'.encode()).hexdigest()


def _stored_record(db, run_id, key):
    run = lookup(db, run_id)
    result = json.loads(run['result'])
    accepted = next((row for row in result['accepted'] if row['key'] == key), None)
    if accepted is None:
        raise DataError('CORRECTION_EMPTY', f'批次 {run_id} 没有业务键 {key} 的签核记录')
    # 原批次里该键生效的归一记录，重新解析原始来源取得业务字段
    raw = stages(run['source'])
    contract = next(row for row in raw['quality']['ready']
                    if row['key'] == key and row['revision'] == accepted['revision'])
    return accepted, contract


def _recompute(fields):
    amount, proof = evaluate(fields)
    return (format(amount, 'f'),
            [{'metric': label, 'value': str(value), 'unit': unit} for label, value, unit in proof])


def _candidates(report):
    """补正文件中每个业务键的最高修订候选，以及其能否直接采信。"""
    ready = {row['key']: row for row in report['quality']['ready']}
    pending = {}
    for row in report['rejected']:
        key = row.get('key')
        if not key or key in ready:
            continue
        revision = row.get('revision')
        if revision is None:
            continue
        previous = pending.get(key)
        if previous is None or revision > previous['revision']:
            pending[key] = {'revision': revision, 'raw': row['raw'], 'code': row['code']}
    return ready, pending


def submit(source):
    report = stages(source)
    ready, pending_only = _candidates(report)
    if not ready and not pending_only:
        raise DataError('CORRECTION_EMPTY', '补正文件没有可采信的最高修订记录')
    source_hash = sha256(source).hexdigest()
    cid = correction_id(source_hash)
    created = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT result FROM corrections WHERE id=?', (cid,)).fetchone()
        if existing is not None:
            return json.loads(existing['result'])
        signed = signed_runs_for_keys(db, ready.keys() | pending_only.keys())
        impacts = []
        for run_id, keys in sorted(signed.items()):
            for key in sorted(keys):
                accepted, contract = _stored_record(db, run_id, key)
                if key in pending_only:
                    candidate = pending_only[key]
                    impacts.append({'correction_id': cid, 'run_id': run_id, 'key': key,
                                    'status': 'pending', 'reason_code': candidate['code'],
                                    'old_revision': accepted['revision'],
                                    'new_revision': candidate['revision'],
                                    'changed_fields': [], 'old_amount': accepted['amount'],
                                    'new_amount': None, 'old_raw': accepted['raw'],
                                    'new_raw': candidate['raw'], 'new_proof': []})
                    continue
                corrected = ready[key]
                base_impact = {'correction_id': cid, 'run_id': run_id, 'key': key,
                               'old_revision': accepted['revision'],
                               'new_revision': corrected['revision'],
                               'old_amount': accepted['amount'], 'old_raw': accepted['raw'],
                               'new_raw': corrected['raw']}
                changed_fields = [field for field in FIELDS
                                  if contract['fields'][field] != corrected['fields'][field]]
                if not changed_fields:
                    impacts.append({**base_impact, 'status': 'kept', 'reason_code': '',
                                    'changed_fields': [], 'new_amount': accepted['amount'],
                                    'new_proof': accepted['proof']})
                    continue
                if corrected['revision'] <= accepted['revision']:
                    impacts.append({**base_impact, 'status': 'pending', 'reason_code': 'REV_BLOCKED',
                                    'changed_fields': changed_fields, 'new_amount': None, 'new_proof': []})
                    continue
                try:
                    new_amount, new_proof = _recompute(corrected['fields'])
                except DataError as error:
                    impacts.append({**base_impact, 'status': 'pending', 'reason_code': error.code,
                                    'changed_fields': changed_fields, 'new_amount': None, 'new_proof': []})
                    continue
                impacts.append({**base_impact, 'status': 'invalidated', 'reason_code': '',
                                'changed_fields': changed_fields,
                                'new_amount': new_amount, 'new_proof': new_proof})
        if any(item['status'] == 'invalidated' for item in impacts):
            status = 'applied'
        elif any(item['status'] == 'pending' for item in impacts):
            status = 'pending'
        else:
            status = 'no_change'
        result = {'id': cid, 'status': status, 'source_sha256': source_hash,
                  'created': created, 'impacts': impacts,
                  'quarantine': report['rejected'], 'suppressed': report['quality']['suppressed']}
        persist(db, cid, source, source_hash, status, created, result, impacts)
        return json.loads(json.dumps(result, ensure_ascii=False))


def list_corrections():
    with connection() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,status,source_sha256,created FROM corrections ORDER BY created DESC,id DESC LIMIT 50')]


def detail(correction_id):
    with connection() as db:
        row = fetch(db, correction_id)
        result = json.loads(row['result'])
        result['impacts'] = load_impacts(db, correction_id)
        return result


def impacts_for_run(db, run_id):
    rows = db.execute("""
        SELECT c.id AS correction_id, i.* FROM correction_impacts i
        JOIN corrections c ON c.id = i.correction_id
        WHERE i.run_id=? ORDER BY c.created DESC, i.key
    """, (run_id,)).fetchall()
    impacts = []
    for row in rows:
        item = dict(row)
        item['changed_fields'] = json.loads(item['changed_fields'])
        item['new_proof'] = json.loads(item['new_proof'])
        item['old_raw'] = json.loads(item['old_raw'])
        item['new_raw'] = json.loads(item['new_raw'])
        impacts.append(item)
    return impacts
