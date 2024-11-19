from datetime import datetime
from typing import Optional
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel, ConfigDict

Base = declarative_base()


class ConversationDB(Base):
    """SQLAlchemy model for conversations."""
    __tablename__ = "conversations"

    conversation_id = Column(String, primary_key=True)
    phone_number = Column(String, nullable=False, index=True)
    messages = Column(Text, nullable=False)  # JSON-encoded list of messages
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<Conversation(phone_number={self.phone_number}, conversation_id={self.conversation_id})>"

class ConversationCreate(BaseModel):
    """Pydantic model for creating a conversation."""
    model_config = ConfigDict(from_attributes=True)

    conversation_id: str
    phone_number: str
    messages: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class ConversationRead(BaseModel):
    """Pydantic model for reading a conversation."""
    model_config = ConfigDict(from_attributes=True)

    conversation_id: str
    phone_number: str
    messages: str
    created_at: datetime
    updated_at: datetime

def get_engine(db_path: str):
    """Create SQLAlchemy engine with the correct URI format."""
    if db_path == "file::memory:?cache=shared":
        return create_engine("sqlite:///:memory:", echo=False)
    return create_engine(f"sqlite:///{db_path}", echo=False)

def get_session(engine) -> Session:
    """Create a new SQLAlchemy session."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()
