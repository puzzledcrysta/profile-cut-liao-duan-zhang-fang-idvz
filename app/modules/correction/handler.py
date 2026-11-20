from fastapi import APIRouter
from app.inputs import Source
from .repository import history
from .service import detail, submit

router = APIRouter()

@router.post('/api/corrections')
def create(source: Source):
    return submit(source.raw())

@router.get('/api/corrections')
def list_all():
    return history()

@router.get('/api/corrections/{correction_id}')
def show(correction_id: str):
    return detail(correction_id)
