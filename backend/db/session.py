"""
DocuMotion - SQLAlchemy Session 관리
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from backend.core.config import DATABASE_URL, DATA_DIR

# 데이터 디렉토리 생성 보장
DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI Depends용 DB 세션 의존성"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """앱 시작 시 테이블 생성 (순환 import 방지를 위해 함수 내에서 import)"""
    from backend.db.models import Project, Slide  # noqa: F401
    Base.metadata.create_all(bind=engine)
