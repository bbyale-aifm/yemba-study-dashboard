from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import calendar as calendar_lib
import re
import ssl
from difflib import SequenceMatcher
from urllib.request import Request as URLRequest, urlopen
from email.utils import parsedate_to_datetime

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db, SessionLocal
from app.models import Assignment, AssignmentStatus, Course, Resource, Workspace
from sqlalchemy import func
import zipfile
from xml.etree import ElementTree
from pypdf import PdfReader
import certifi
from app.flow import seed_flow

BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_FILE = BASE_DIR.parent / "data" / "class_weekend_schedule.json"
CANVAS_FEED_URL = "https://yale.instructure.com/feeds/calendars/user_U6bzA9TFrph60tflw0HegvUc6worRfFZ4ZVIIfZP.ics"
CANVAS_IGNORED_TITLE_FRAGMENTS = ("game theory problem set 2", "review session moved to 7 30 pm", "consumer choice exercise individual mgt 411 e1")
last_canvas_sync = {"status": "not_synced", "updated": 0, "ignored": 0, "at": None, "error": "", "message": "Canvas sync has not been started yet."}


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def deduplicate_assignments(db: Session) -> int:
    items = db.scalars(select(Assignment).order_by(Assignment.due_at.asc())).all()
    kept, removed = [], 0
    for item in items:
        category = (item.description or "Assignment").split(" · ", 1)[0]
        duplicate = next((prior for prior in kept
            if prior.course_id == item.course_id
            and (prior.description or "Assignment").split(" · ", 1)[0] == category
            and (normalized_title(prior.title) == normalized_title(item.title)
                 or (SequenceMatcher(None, normalized_title(prior.title), normalized_title(item.title)).ratio() >= .92
                     and abs((utc_datetime(prior.due_at) - utc_datetime(item.due_at)).total_seconds()) <= 3 * 86400))), None)
        if duplicate:
            if item.status == AssignmentStatus.complete and duplicate.status != AssignmentStatus.complete:
                duplicate.status = item.status
            db.delete(item); removed += 1
        else:
            kept.append(item)
    return removed


def canvas_category(title: str) -> str:
    lowered = title.lower()
    if "review" in lowered or "office hour" in lowered:
        return "Review Session"
    return "Assignment"


def resolve_canvas_course(title: str, courses: list[Course]) -> Course | None:
    code = re.search(r"MGT\s+\d+", title, re.IGNORECASE)
    if code:
        wanted = code.group(0).upper()
        return next((course for course in courses if course.code.upper() == wanted), None)
    title_words = set(normalized_title(title).split())
    matches = [course for course in courses if len(title_words & set(normalized_title(course.name).split())) >= 2]
    return matches[0] if len(matches) == 1 else None


def sync_canvas_feed(db: Session) -> int:
    """Refresh Canvas-owned calendar records while preserving local readings."""
    try:
        request = URLRequest(CANVAS_FEED_URL, headers={"User-Agent": "EMBA Study Hub"})
        tls_context = ssl.create_default_context(cafile=certifi.where())
        raw = urlopen(request, timeout=8, context=tls_context).read().decode("utf-8", errors="replace")
    except Exception:
        last_canvas_sync.update({
            "status": "not_connected",
            "error": "Canvas feed could not be reached",
            "at": datetime.now(timezone.utc).isoformat(),
            "message": "Canvas is not connected yet. Open Canvas or connect your course feed to enable syncing.",
        })
        return 0
    events = []
    for block in raw.split("BEGIN:VEVENT")[1:]:
        fields = {}
        for line in block.split("END:VEVENT", 1)[0].splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.split(";", 1)[0]] = value.replace("\\,", ",").replace("\\n", " ")
        if fields.get("SUMMARY") and fields.get("DTSTART"):
            stamp = fields["DTSTART"].replace("Z", "+0000")
            try:
                due = datetime.strptime(stamp[:8], "%Y%m%d").replace(tzinfo=timezone.utc) if "T" not in stamp else datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            events.append((fields["SUMMARY"], due, fields.get("LOCATION", "")))
    updated = 0
    ignored = 0
    workspace = db.scalar(select(Workspace).limit(1))
    courses = db.scalars(select(Course)).all()
    for title, due, location in events:
        if any(fragment in normalized_title(title) for fragment in CANVAS_IGNORED_TITLE_FRAGMENTS):
            continue
        item = db.scalar(select(Assignment).where(Assignment.title == title).limit(1))
        course = resolve_canvas_course(title, courses)
        category = canvas_category(title)
        if not item and course:
            candidates = db.scalars(select(Assignment).where(Assignment.course_id == course.id)).all()
            item = next((candidate for candidate in candidates if (candidate.description or "Assignment").split(" · ", 1)[0] == category and SequenceMatcher(None, normalized_title(candidate.title), normalized_title(title)).ratio() >= .80 and abs((utc_datetime(candidate.due_at) - utc_datetime(due)).total_seconds()) <= 14 * 86400), None)
        if item:
            if not item.course_id and course:
                item.course_id = course.id
            item.due_at = due
            item.description = f"{(item.description or category + ' · Canvas').split(' · ', 1)[0]} · {location or 'Canvas'}"
            updated += 1
        elif workspace and course:
            db.add(Assignment(workspace_id=workspace.id, course_id=course.id, title=title, description=f"{category} · {location or 'Canvas'}", due_at=due, status=AssignmentStatus.not_started, priority="normal"))
            updated += 1
        else:
            ignored += 1
    updated += deduplicate_assignments(db)
    db.commit()
    last_canvas_sync.update({
        "status": "success",
        "updated": updated,
        "ignored": ignored,
        "at": datetime.now(timezone.utc).isoformat(),
        "error": "",
        "message": f"Canvas calendar synced. {ignored} ambiguous item(s) were not imported.",
    })
    return updated
app = FastAPI(title="EMBA Study Dashboard", version="0.1.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def create_local_schema() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_flow(db)


@app.post("/api/canvas/sync")
def canvas_sync(db: Session = Depends(get_db)) -> dict[str, int | str]:
    updated = sync_canvas_feed(db)
    payload = {**last_canvas_sync, "updated": updated}
    if payload["status"] == "not_connected":
        payload["message"] = "Canvas is not connected yet. Open Canvas or connect your course feed to enable syncing."
    elif payload["status"] == "success":
        payload["message"] = "Canvas calendar synced successfully."
    else:
        payload["message"] = "Canvas sync has not been started yet."
    payload["error"] = payload.get("error") or ""
    return payload


def extract_syllabus_text(filename: str, content: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        import io
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    if filename.lower().endswith(".docx"):
        with zipfile.ZipFile(__import__('io').BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
    raise ValueError("Please upload a PDF or Word document.")


def parse_syllabus_date(text: str) -> datetime | None:
    patterns = [
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*|\s+)(\d{4})\b",
        r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match: continue
        try:
            if pattern.startswith(r"\b(January"):
                return datetime.strptime(" ".join(match.groups()), "%B %d %Y").replace(tzinfo=timezone.utc)
            year = int(match.group(3)); year += 2000 if year < 100 else 0
            return datetime(year, int(match.group(1)), int(match.group(2)), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@app.post("/api/syllabus")
async def upload_syllabus(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Syllabus files must be 10 MB or smaller.")
    if not (file.filename or "").lower().endswith((".pdf", ".docx")):
        raise HTTPException(status_code=415, detail="Please upload a PDF or Word document.")
    text_content = extract_syllabus_text(file.filename or "", content)
    code_match = re.search(r"MGT\s*\d{3}[A-Z]?", text_content, re.IGNORECASE)
    code = code_match.group(0).upper().replace(" ", " ") if code_match else "NEW"
    name_match = re.search(r"(?:MGT\s*\d{3}[A-Z]?\s*[-:–]?\s*)([^\n]{3,80})", text_content, re.IGNORECASE)
    name = name_match.group(1).strip(" -:–") if name_match else (file.filename or "New class").rsplit(".", 1)[0]
    existing = db.scalar(select(Course).where(func.lower(Course.code) == code.lower()).limit(1)) if code != "NEW" else None
    if existing:
        return {"status": "existing", "message": "This class is already accounted for in the study hub"}
    workspace = db.scalar(select(Workspace).limit(1))
    course = Course(workspace_id=workspace.id, code=code, name=name)
    db.add(course); db.flush()
    added = {"required": 0, "suggested": 0, "assignments": 0, "review_sessions": 0}
    needs_review = []
    for raw_line in text_content.splitlines():
        line = " ".join(raw_line.split()).strip("•-* ")
        lower = line.lower()
        if not line or len(line) < 4: continue
        parsed_date = parse_syllabus_date(line)
        if "suggested reading" in lower or "optional reading" in lower:
            db.add(Resource(workspace_id=workspace.id, course_id=course.id, title=line, resource_type="suggested_reading")); added["suggested"] += 1
        elif "required reading" in lower or "reading:" in lower:
            db.add(Resource(workspace_id=workspace.id, course_id=course.id, title=line, resource_type="required_reading")); added["required"] += 1
        elif "review" in lower or "office hour" in lower:
            if parsed_date: db.add(Assignment(workspace_id=workspace.id, course_id=course.id, title=line, description="Review Session · Syllabus", due_at=parsed_date, priority="normal")); added["review_sessions"] += 1
            else: needs_review.append(line)
        elif "assignment" in lower or "due" in lower:
            if parsed_date: db.add(Assignment(workspace_id=workspace.id, course_id=course.id, title=line, description="Assignment · Syllabus", due_at=parsed_date, priority="normal")); added["assignments"] += 1
            else: needs_review.append(line)
    db.commit()
    return {"status": "created", "message": f"Added {course.code} · {course.name} to the study hub", "added": added, "needs_review": needs_review[:20]}


@app.post("/api/study-assistant/chat")
def study_assistant_chat(payload: dict, db: Session = Depends(get_db)) -> dict[str, str]:
    message = str(payload.get("message", "")).strip().lower()
    profile = payload.get("profile") or {}
    name = str(profile.get("name", "")).strip()
    focus = str(profile.get("focus", "")).strip()
    history = payload.get("history") or []
    upcoming = [item for item in db.scalars(select(Assignment).order_by(Assignment.due_at.asc())).all() if (item.due_at.replace(tzinfo=timezone.utc) if item.due_at.tzinfo is None else item.due_at) >= datetime.now(timezone.utc)][:5]
    if not upcoming:
        reply = "You have no upcoming imported deadlines. Tell me what you want to accomplish and I’ll help shape a plan."
    elif "weekend" in message or "week" in message or "priorit" in message or "plan" in message:
        tasks = "; ".join(f"{item.title} ({item.due_at.strftime('%b %d')})" for item in upcoming[:4])
        reply = f"Start with the nearest deadlines, then protect time for readings. Your next priorities are: {tasks}. I’d use three focused blocks: urgent assignment work, required reading, then review or catch-up."
    else:
        reply = f"I can help prioritize your study time. Your next deadline is {upcoming[0].title} on {upcoming[0].due_at.strftime('%b %d')}. Ask me to plan your weekend, plan the week, or prioritize a course."
    if focus:
        reply += f" I’ll keep your focus on {focus} in mind."
    if name:
        reply = f"{name}, " + reply[0].lower() + reply[1:]
    if history and message in {"thanks", "thank you", "ok", "okay"}:
        reply += " Want me to turn that into a time-blocked plan?"
    return {"reply": reply}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/assignments")
def list_assignments(
    db: Session = Depends(get_db),
    status: AssignmentStatus | None = Query(default=None),
) -> list[dict]:
    statement = select(Assignment).order_by(Assignment.due_at.asc())
    if status:
        statement = statement.where(Assignment.status == status)
    assignments = db.scalars(statement).all()
    return [
        {
            "id": assignment.id,
            "title": assignment.title,
            "due_at": assignment.due_at,
            "status": assignment.status,
            "priority": assignment.priority,
            "course_id": assignment.course_id,
        }
        for assignment in assignments
    ]


@app.get("/api/calendar")
def calendar_items(month: str | None = None, term: str = "all", db: Session = Depends(get_db)) -> list[dict]:
    """Return calendar items for incremental month-by-month clients."""
    items = db.scalars(select(Assignment).order_by(Assignment.due_at.asc())).all()
    if month:
        items = [item for item in items if item.due_at.strftime("%Y-%m") == month]
    return [{"id": item.id, "title": item.title, "due_at": item.due_at, "description": item.description, "course_id": item.course_id} for item in items]


@app.patch("/api/assignments/{assignment_id}/status")
def update_assignment_status(assignment_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    item = db.get(Assignment, assignment_id)
    if not item:
        return {"status": "not_found"}
    item.status = AssignmentStatus(payload.get("status", "not_started"))
    db.commit()
    return {"status": item.status.value}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    local_date = now.astimezone().date()
    if local_date.weekday() >= 5:
        agenda_start = local_date - timedelta(days=local_date.weekday() - 5)
        agenda_end = agenda_start + timedelta(days=1)
        agenda_label = "Weekend agenda"
    else:
        agenda_start = local_date - timedelta(days=local_date.weekday())
        agenda_end = agenda_start + timedelta(days=4)
        agenda_label = "Week agenda"
    courses = db.scalars(select(Course)).all()
    term_map = {'MGT 404': 'Summer/Fall 2026', 'MGT 401': 'Summer/Fall 2026', 'MGT 406': 'Summer 2026', 'MGT 402': 'Summer 2026'}
    term_map['C28 Hub'] = None
    course_terms = {course.id: term_map.get(course.code, 'Fall 2026') for course in courses}
    selected_terms = [term for term in request.query_params.get('terms', '').split(',') if term]
    selected_term = selected_terms[0] if len(selected_terms) == 1 else 'all'
    allowed_ids = {course_id for course_id, term in course_terms.items() if not selected_terms or term in selected_terms or term is None}
    resources = db.scalars(select(Resource)).all()
    course_names = {course.id: f"{course.code} · {course.name}" for course in courses if course.id in allowed_ids}
    calendar_items = [item for item in db.scalars(select(Assignment).order_by(Assignment.due_at.asc())).all() if item.course_id in allowed_ids or item.course_id is None]
    course_materials = {}
    for item in calendar_items:
        category = (item.description or '').split(' · ', 1)[0]
        if category in {'Required Reading', 'Suggested Reading'}:
            course_materials.setdefault(item.course_id, {'Required Reading': [], 'Suggested Reading': []})[category].append(item.title)
    schedule_items = json.loads(SCHEDULE_FILE.read_text())
    location_by_date_course = {(item['date'], item['course_code']): item['location'] for item in schedule_items}
    course_codes = {course.id: course.code for course in courses}
    calendar_locations = {item.id: location_by_date_course.get((item.due_at.date().isoformat(), course_codes.get(item.course_id, '')), '') for item in calendar_items}
    schedule_months = sorted({item['date'][:7] for item in schedule_items})
    schedule_by_date = {}
    for item in schedule_items:
        schedule_by_date.setdefault(item['date'], []).append(item)
    schedule_calendar_months = []
    for year, month in sorted({(int(item['date'][:4]), int(item['date'][5:7])) for item in schedule_items}):
        weeks = []
        for week in calendar_lib.Calendar(firstweekday=6).monthdayscalendar(year, month):
            weeks.append([{'day': day, 'items': schedule_by_date.get(f'{year:04d}-{month:02d}-{day:02d}', [])} if day else {'day': 0, 'items': []} for day in week])
        schedule_calendar_months.append({'key': f'{year:04d}-{month:02d}', 'label': calendar_lib.month_name[month] + f' {year}', 'weeks': weeks})
    calendar_by_date = {}
    for item in calendar_items:
        calendar_by_date.setdefault(item.due_at.date().isoformat(), []).append(item)
    calendar_months = []
    for year, month in sorted({(item.due_at.year, item.due_at.month) for item in calendar_items}):
        weeks = []
        for week in calendar_lib.Calendar(firstweekday=6).monthdayscalendar(year, month):
            weeks.append([{'day': day, 'items': calendar_by_date.get(f'{year:04d}-{month:02d}-{day:02d}', [])} if day else {'day': 0, 'items': []} for day in week])
        calendar_months.append({'key': f'{year:04d}-{month:02d}', 'label': calendar_lib.month_name[month] + f' {year}', 'weeks': weeks})
    upcoming = [item for item in calendar_items if (item.due_at.replace(tzinfo=timezone.utc) if item.due_at.tzinfo is None else item.due_at) >= now and item.status != AssignmentStatus.complete][:8]
    upcoming = [item for item in upcoming if not (item.description or '').startswith('Class ·')]
    deadline_items = [item for item in calendar_items if not (item.description or '').startswith('Class ·') and (item.due_at.replace(tzinfo=timezone.utc) if item.due_at.tzinfo is None else item.due_at) >= now]
    completed_deadlines = sum(item.status == AssignmentStatus.complete for item in deadline_items)
    completion_percent = round((completed_deadlines / len(deadline_items)) * 100) if deadline_items else 0
    agenda_items = upcoming[:4]
    calendar_only = request.query_params.get('view') == 'calendar'
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "assignments": upcoming,
            "today": now.date(),
            "week_end": (now + timedelta(days=7)).date(),
            "agenda_label": agenda_label,
            "agenda_start": agenda_start,
            "agenda_end": agenda_end,
            "course_names": course_names,
            "courses": courses,
            "active_courses": len(allowed_ids),
            "vaulted_materials": len(resources),
            "audio_briefs": sum(resource.resource_type.lower() == "notebooklm" for resource in resources),
            "calendar_items": calendar_items,
            "agenda_items": agenda_items,
            "completion_percent": completion_percent,
            "schedule_items": schedule_items,
            "schedule_months": schedule_months,
            "schedule_calendar_months": schedule_calendar_months,
            "calendar_months": calendar_months,
            "calendar_only": calendar_only,
            "course_materials": course_materials,
            "current_calendar_month": now.strftime('%Y-%m'),
            "course_terms": course_terms,
            "term_options": sorted({term for term in course_terms.values() if term}, key=lambda term: {'Summer 2026': 0, 'Summer/Fall 2026': 1, 'Fall 2026': 2}.get(term, 99)),
            "selected_term": selected_term,
            "selected_terms": selected_terms,
            "calendar_locations": calendar_locations,
        },
    )
