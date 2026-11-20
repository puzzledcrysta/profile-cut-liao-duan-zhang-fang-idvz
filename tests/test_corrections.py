import csv
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.store import connection
from app.modules.correction.service import submit

ROOT = Path(__file__).resolve().parents[1]
SHIFT = (ROOT / 'sample_data/shift.csv').read_text(encoding='utf-8-sig')


def correction_csv(rows):
    """rows: list of dict-like modifications over the first shift.csv line."""
    reader = csv.reader(io.StringIO(SHIFT))
    header, base = next(reader), next(reader)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(header)
    for changes in rows:
        line = base.copy()
        for field, value in changes.items():
            line[header.index(field)] = value
        writer.writerow(line)
    return stream.getvalue().encode()


def signed(client, anchor):
    review = client.post(f'/api/review/{anchor}', json={'version': 0, 'decision': '确认', 'note': '已逐条核对'})
    assert review.status_code == 200


def post_correction(client, payload):
    return client.post('/api/corrections', json=payload)


def test_correction_invalidates_only_changed_keys_of_signed_batch(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    changed = correction_csv([
        {'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'},
        {'锯切计划': '00102', '修订号': '3', '切件规格': '3000x4'},
    ])
    report = post_correction(client, {'data': changed.decode()}).json()
    assert report['status'] == 'applied'
    keys = sorted(item['key'] for item in report['impacts'])
    assert keys == ['00101', '00102']
    by_key = {item['key']: item for item in report['impacts']}
    assert by_key['00101']['changed_fields'] == ['切件规格']
    # 00102 原生效记录的切件规格本就是 3000x4，补正只动了其余业务字段
    assert set(by_key['00102']['changed_fields']) == {'锯缝毫米', '回用下限毫米', '余料清单'}
    for item in report['impacts']:
        assert item['run_id'] == anchor and item['status'] == 'invalidated'
        assert item['old_amount'] != item['new_amount'] and item['new_proof']


def test_unsigned_batch_is_not_affected(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    changed = correction_csv([{'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'}])
    report = post_correction(client, {'data': changed.decode()}).json()
    assert report['impacts'] == []


def test_same_business_fields_keep_original_signoff(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    # 业务字段完全一致，只改了时间和备注：不算实际改动
    same = correction_csv([{
        '锯切计划': '00101', '修订号': '2',
        '提交时间': '2026-11-20T10:00:00+08:00', '原始备注': '仅换时间戳',
    }])
    report = post_correction(client, {'data': same.decode()}).json()
    assert report['status'] == 'no_change'
    kept, = report['impacts']
    assert (kept['key'], kept['status'], kept['changed_fields']) == ('00101', 'kept', [])
    detail = client.get(f'/api/runs/{anchor}').json()
    assert next(row for row in detail['accepted'] if row['key'] == '00101')['amount'] == '0'


def test_recomputed_amount_and_proof_follow_corrected_record(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    corrected = correction_csv([{'锯切计划': '00103', '修订号': '2', '切件规格': '6000x2'}])
    report = post_correction(client, {'data': corrected.decode()}).json()
    impact, = report['impacts']
    # 两支 6000 毫米切件需要两根新棒
    assert impact['new_amount'] == '12000'
    assert impact['old_amount'] == '6000'
    assert any(step['metric'] == '投入新料' and step['value'] == '12000' for step in impact['new_proof'])
    detail = client.get(f'/api/corrections').json()
    assert detail[0]['id'] == report['id'] and detail[0]['status'] == 'applied'
    full = client.get(f"/api/corrections/{report['id']}").json()
    assert full['impacts'][0]['new_amount'] == '12000'


def test_same_revision_conflict_only_pending_never_overwrites(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    reader = csv.reader(io.StringIO(SHIFT))
    header = next(reader)
    base = next(reader)
    first, second = base.copy(), base.copy()
    for row, spec in ((first, '2000x2'), (second, '1500x2')):
        row[header.index('锯切计划')] = '00104'
        row[header.index('修订号')] = '2'
        row[header.index('切件规格')] = spec
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows([header, first, second])
    report = post_correction(client, {'data': stream.getvalue()}).json()
    assert report['status'] == 'pending'
    pending, = report['impacts']
    assert (pending['key'], pending['status'], pending['reason_code']) == ('00104', 'pending', 'REV_CONFLICT')
    run = client.get(f'/api/runs/{anchor}').json()
    # 原签核结论不得被覆盖
    assert next(row for row in run['accepted'] if row['key'] == '00104')['amount'] == '12000'


def test_repeated_correction_is_idempotent(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    payload = {'data': correction_csv([
        {'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'}]).decode()}
    first = post_correction(client, payload).json()
    second = post_correction(client, payload).json()
    assert first['id'] == second['id'] and first == second
    assert len(client.get('/api/corrections').json()) == 1
    with connection() as db:
        assert db.execute('SELECT COUNT(*) FROM correction_impacts').fetchone()[0] == 1


def test_concurrent_corrections_write_one_record(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    payload = correction_csv([
        {'锯切计划': '00105', '修订号': '2', '切件规格': '3000x2'}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(payload), range(4)))
    assert all(item['id'] == results[0]['id'] for item in results)
    assert len(client.get('/api/corrections').json()) == 1
    with connection() as db:
        assert db.execute('SELECT COUNT(*) FROM correction_impacts').fetchone()[0] == 1


def test_original_signoff_text_remains_queryable(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    payload = correction_csv([{'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'}])
    report = post_correction(client, {'data': payload.decode()}).json()
    impact, = report['impacts']
    assert impact['old_amount'] == '0'
    assert impact['old_raw'] and '2000x1;1500x1' in impact['old_raw']
    reviews = client.get(f'/api/review/{anchor}').json()
    assert reviews[0]['decision'] == '确认' and reviews[0]['note'] == '已逐条核对'
    run = client.get(f'/api/runs/{anchor}').json()
    # 原批次证据仍保留原行文本
    assert any('2000x1;1500x1' in row['raw'] for row in run['accepted'] if row['key'] == '00101')


def test_correction_touches_every_signed_batch_holding_the_key(client, book):
    first_anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, first_anchor)
    # 另一对输入形成另一个批次，也含 00101
    other_source = correction_csv([
        {'锯切计划': '00101', '修订号': '1'},
        {'锯切计划': '00999', '修订号': '1', '标准棒料毫米': '6000', '锯缝毫米': '3',
         '回用下限毫米': '100', '余料清单': '无', '切件规格': '3000x1', '提交时间': '2026-11-18T09:30:00+08:00'}])
    second = client.post('/api/runs', json={'data': other_source.decode()}).json()
    second_anchor = second['manifest']['run_id']
    signed(client, second_anchor)
    corrected = correction_csv([{'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'}])
    report = post_correction(client, {'data': corrected.decode()}).json()
    assert sorted(item['run_id'] for item in report['impacts']) == sorted([first_anchor, second_anchor])
    assert all(item['key'] == '00101' and item['status'] == 'invalidated' for item in report['impacts'])


def test_correction_without_accepted_records_is_rejected(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    # 主键缺失：归一阶段即拒绝，补正文件没有任何业务键候选
    bad = correction_csv([{'锯切计划': 'N/A', '修订号': '2', '切件规格': 'N/A'}])
    response = post_correction(client, {'data': bad.decode()})
    assert response.status_code == 422 and response.json()['code'] == 'CORRECTION_EMPTY'
    assert client.get('/api/corrections').json() == []


def test_run_detail_and_export_carry_correction_outcome(client):
    anchor = client.post('/api/runs', json={}).json()['manifest']['run_id']
    signed(client, anchor)
    payload = correction_csv([{'锯切计划': '00101', '修订号': '2', '切件规格': '2000x2'}])
    report = post_correction(client, {'data': payload.decode()}).json()
    run = client.get(f'/api/runs/{anchor}').json()
    outcome, = run['corrections']
    assert outcome['correction_id'] == report['id']
    assert outcome['key'] == '00101' and outcome['status'] == 'invalidated'
    assert outcome['new_amount']
    exported = client.get(f'/api/export/{anchor}').json()
    assert exported['corrections'][0]['key'] == '00101'
    # 导出主体仍是原签核原文
    assert any(row['key'] == '00101' and row['amount'] == '0' for row in exported['results'])
