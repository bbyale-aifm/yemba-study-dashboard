"""The small, explicit starter flow used by the local dashboard.

The JSON file is intentionally human-editable.  A future Canvas/calendar
importer can produce the same shape without changing the dashboard.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Assignment, AssignmentStatus, Course, User, Workspace, WorkspaceMember

FLOW_FILE = Path(__file__).resolve().parent.parent / "data" / "canvas_calendar.json"


def load_flow() -> dict:
    return json.loads(FLOW_FILE.read_text())


def seed_flow(db: Session) -> None:
    """Create the local workspace from the imported Canvas calendar data."""
    existing = db.scalar(select(Workspace).limit(1))
    if existing:
        if db.scalar(select(Course.id).where(Course.workspace_id == existing.id, Course.code == "MGT 412")):
            db.query(Assignment).filter(Assignment.workspace_id == existing.id).delete()
            db.query(Course).filter(Course.workspace_id == existing.id).delete()
            db.commit()
        elif db.scalar(select(Course.id).where(Course.workspace_id == existing.id)):
            return
        else:
            flow = load_flow()
            workspace = existing
            user = db.scalar(select(User).limit(1))
            course_ids = {}
            for item in flow["courses"]:
                course = Course(workspace_id=workspace.id, code=item["code"], name=item["name"])
                db.add(course)
                db.flush()
                course_ids[item["code"]] = course.id
            for item in flow["assignments"]:
                db.add(Assignment(workspace_id=workspace.id, course_id=course_ids.get(item.get("course_code")), title=item["title"], description=item.get("description"), due_at=datetime.fromisoformat(item["due_at"]), status=AssignmentStatus(item.get("status", "not_started")), priority=item.get("priority", "normal")))
            db.commit()
            return
    flow = load_flow()
    user = User(email=flow["user"]["email"], display_name=flow["user"]["display_name"])
    workspace = Workspace(name=flow["workspace"]["name"], slug=flow["workspace"]["slug"])
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    course_ids = {}
    for item in flow["courses"]:
        course = Course(workspace_id=workspace.id, code=item["code"], name=item["name"])
        db.add(course)
        db.flush()
        course_ids[item["code"]] = course.id
    for item in flow["assignments"]:
        db.add(Assignment(
            workspace_id=workspace.id,
            course_id=course_ids.get(item.get("course_code")),
            title=item["title"],
            description=item.get("description"),
            due_at=datetime.fromisoformat(item["due_at"]),
            status=AssignmentStatus(item.get("status", "not_started")),
            priority=item.get("priority", "normal"),
        ))
    db.commit()
