"""
DocuMotion - 자주 쓰는 장소 (SavedLocation) API

전역 저장 위치(집/회사 등) CRUD. 경로/장소 슬라이드 생성 시 빠른 선택용.
저장 시 map_service.geocode 로 좌표를 검증·캐싱한다.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.models import SavedLocation
from backend.db.session import get_db
from backend.services import map_service
from backend.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


class LocationRead(BaseModel):
    id: str
    name: str
    query: str
    lat: float
    lng: float

    class Config:
        from_attributes = True


class LocationCreate(BaseModel):
    name: str
    query: str


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    query: Optional[str] = None


def _geocode_or_400(query: str) -> dict:
    """query → {lat, lng}. 실패 시 400."""
    try:
        geo = map_service.geocode(query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"SavedLocation geocode 실패 ({query!r}): {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"장소 확인 실패: {e}")
    return {"lat": geo["lat"], "lng": geo["lng"]}


@router.get("/locations", response_model=List[LocationRead])
def list_locations(db: Session = Depends(get_db)):
    """저장된 장소 목록 (이름순)."""
    return db.query(SavedLocation).order_by(SavedLocation.name).all()


@router.post("/locations", response_model=LocationRead, status_code=201)
def create_location(payload: LocationCreate, db: Session = Depends(get_db)):
    """새 장소 저장. query 를 geocode 로 검증하고 좌표 캐싱."""
    name = (payload.name or "").strip()
    query = (payload.query or "").strip()
    if not name or not query:
        raise HTTPException(status_code=400, detail="이름과 주소를 모두 입력하세요")
    coords = _geocode_or_400(query)
    loc = SavedLocation(name=name, query=query, **coords)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    logger.info(f"SavedLocation created: {name!r} = {query!r} ({coords['lat']:.4f},{coords['lng']:.4f})")
    return loc


@router.put("/locations/{location_id}", response_model=LocationRead)
def update_location(location_id: str, payload: LocationUpdate, db: Session = Depends(get_db)):
    """장소 수정. query 가 바뀌면 재 geocode."""
    loc = db.query(SavedLocation).filter(SavedLocation.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    if payload.name is not None:
        loc.name = payload.name.strip() or loc.name
    if payload.query is not None and payload.query.strip() and payload.query.strip() != loc.query:
        new_query = payload.query.strip()
        coords = _geocode_or_400(new_query)
        loc.query = new_query
        loc.lat, loc.lng = coords["lat"], coords["lng"]
    db.commit()
    db.refresh(loc)
    return loc


@router.delete("/locations/{location_id}", status_code=204)
def delete_location(location_id: str, db: Session = Depends(get_db)):
    loc = db.query(SavedLocation).filter(SavedLocation.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    db.delete(loc)
    db.commit()
