"""
DocuMotion - SQLAlchemy ORM Models
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship

from backend.db.session import Base


class Project(Base):
    __tablename__ = "projects"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String(200), nullable=False)
    status     = Column(String(20), default="DRAFT")   # DRAFT / QUEUED / PROCESSING / COMPLETED / ERROR
    stage      = Column(String(20), default="initialized")  # initialized / uploaded / scripted
    progress   = Column(Integer, default=0)
    message    = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    slides = relationship("Slide", back_populates="project",
                          cascade="all, delete-orphan",
                          order_by="Slide.order_index")


class Slide(Base):
    __tablename__ = "slides"

    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id     = Column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    order_index    = Column(Integer, nullable=False, default=0)
    image_filename = Column(String(300), default="")
    label          = Column(String(300), default="")
    text           = Column(Text, default="")

    project = relationship("Project", back_populates="slides")
