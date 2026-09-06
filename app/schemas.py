from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    due_at: datetime
    status: str
    priority: str
    course_id: str | None


class ResourceCreate(BaseModel):
    title: str
    resource_type: str
    url: str | None = None
    notes: str | None = None
    course_id: str | None = None


class CalendarEventRead(BaseModel):
    title: str
    starts_at: datetime
    ends_at: datetime
    source_url: str | None = None
