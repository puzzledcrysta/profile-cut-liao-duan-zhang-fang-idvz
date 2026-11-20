import json
from app.store import connection
from app.modules.runs.service import get
from app.modules.correction.service import impacts_for_run
from .decisions import timeline


def bundle(run_id):
    history = timeline(run_id)
    run = get(run_id)
    with connection() as db:
        corrections = impacts_for_run(db, run_id)
    # The export carries source line evidence and the applicable decision version.
    return json.dumps({'manifest':run['manifest'],'counts':run['counts'],'results':run['accepted'],
                       'quarantine':run['quarantine'],'suppressed':run['suppressed'],
                       'reconciliation':run['book'],'review':history,
                       'corrections':corrections},ensure_ascii=False,indent=2)
