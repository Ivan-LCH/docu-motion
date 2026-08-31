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
    from backend.db.models import Project, Slide, SavedLocation  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def _migrate_columns():
    """기존 DB에 새 컬럼이 없으면 ALTER TABLE로 추가 (SQLite 마이그레이션)"""
    import sqlalchemy
    with engine.connect() as conn:
        migrations = [
            ("slides", "tts_volume", "FLOAT DEFAULT 1.0"),
            ("slides", "rotation", "INTEGER DEFAULT 0"),
            ("projects", "tts_master_volume", "FLOAT DEFAULT 1.0"),
            ("slides", "overlays", "TEXT DEFAULT '[]'"),
            ("slides", "image_fit", "TEXT DEFAULT 'cover'"),
            ("slides", "ken_burns", "INTEGER DEFAULT 0"),
            ("projects", "title_text", "TEXT DEFAULT ''"),
            ("slides", "meta", "TEXT DEFAULT '{}'"),
            ("projects", "resolution", "TEXT DEFAULT '720p'"),
            ("projects", "transition_duration", "FLOAT DEFAULT 0.7"),
            ("projects", "style_preset", "TEXT DEFAULT 'none'"),
    ("projects", "tts_voice", "TEXT DEFAULT ''"),
            ("slides", "exif", "TEXT DEFAULT '{}'"),
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(sqlalchemy.text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                conn.execute(sqlalchemy.text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
