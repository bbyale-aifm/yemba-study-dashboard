from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import calendar as calendar_lib
import re
import ssl
from zoneinfo import ZoneInfo
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
CANVAS_IGNORED_TITLE_FRAGMENTS = ("game theory problem set 2", "review session moved to 7 30 pm", "consumer choice exercise individual mgt 411 e1", "attd colloq 9 11 26 mgt 699 e1", "mgt 699 e1 fa26 emba management colloquium jonathan cohn", "practice problems class 1a mgt 410 e1", "game theory final exam mgt 404 e1", "nyt bordeaux equation ai generated followup")
last_canvas_sync = {"status": "not_synced", "updated": 0, "ignored": 0, "at": None, "error": "", "message": "Canvas sync has not been started yet."}
pending_canvas_sync = {}
CANVAS_TIMEZONE = ZoneInfo("America/New_York")


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def parse_canvas_datetime(property_name: str, value: str) -> datetime:
    """Parse Canvas ICS times without shifting the local wall-clock time."""
    tz_match = re.search(r"(?:^|;)TZID=([^;:]+)", property_name, re.IGNORECASE)
    is_utc = value.endswith("Z")
    stamp = value[:-1] if is_utc else value
    fmt = "%Y%m%d" if "T" not in stamp else "%Y%m%dT%H%M%S"
    parsed = datetime.strptime(stamp[:8] if fmt == "%Y%m%d" else stamp[:15], fmt)
    if is_utc:
        return parsed.replace(tzinfo=timezone.utc)
    # Canvas commonly emits a floating DTSTART even though it represents the
    # user's Eastern calendar. An explicit TZID still wins when supplied.
    zone = ZoneInfo(tz_match.group(1)) if tz_match else CANVAS_TIMEZONE
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def display_due_at(item: Assignment) -> datetime:
    """Return the event's actual time, including legacy Canvas imports."""
    due = item.due_at
    if due.hour == 0 and due.minute == 0 and item.description:
        match = re.search(r"(?:^| · )((?:[01]?\d)|2[0-3]):([0-5]\d)\s*([AP]M)?", item.description, re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            meridiem = (match.group(3) or "").upper()
            if meridiem:
                hour = (hour % 12) + (12 if meridiem == "PM" else 0)
            return datetime(due.year, due.month, due.day, hour, minute, tzinfo=CANVAS_TIMEZONE).astimezone(timezone.utc)
    return due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due


def assignment_source(item: Assignment) -> str:
    description = (item.description or '').lower()
    if 'canvas' in description or 'zoom' in description:
        return 'Canvas'
    if 'syllabus' in description:
        return 'Manual import'
    return 'Local entry'


def find_canvas_assignment(db: Session, title: str, course: Course | None, category: str, event_uid: str = '') -> Assignment | None:
    if event_uid:
        item = db.scalar(select(Assignment).where(Assignment.description.ilike(f'%Canvas UID:{event_uid}%')).limit(1))
        if item:
            return item
    item = db.scalar(select(Assignment).where(Assignment.title == title).limit(1))
    if item or not course:
        return item
    wanted = normalized_title(title)
    candidates = db.scalars(select(Assignment).where(Assignment.course_id == course.id)).all()
    return next((candidate for candidate in candidates
        if (candidate.description or 'Assignment').split(' · ', 1)[0] == category
        and (normalized_title(candidate.title).startswith(wanted[:80])
             or wanted.startswith(normalized_title(candidate.title)[:80])
             or SequenceMatcher(None, normalized_title(candidate.title), wanted).ratio() >= .86)), None)


def restore_canvas_sync_status(db: Session) -> None:
    """Keep the sync indicator after a process restart/refresh."""
    if last_canvas_sync["status"] in {"success", "preview"}:
        return
    has_canvas_records = db.scalar(select(Assignment.id).where(Assignment.description.ilike('%Canvas%')).limit(1))
    if has_canvas_records:
        now = datetime.now(timezone.utc).isoformat()
        last_canvas_sync.update({
            "status": "success",
            "at": now,
            "error": "",
            "message": "Canvas records are loaded in this dashboard.",
        })


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


def remove_non_canvas_duplicates(db: Session) -> int:
    """Prefer a Canvas assignment over a same-day Zoom copy."""
    items = db.scalars(select(Assignment).order_by(Assignment.due_at.asc())).all()
    removed = 0
    for item in items:
        item_category = (item.description or "Assignment").split(" · ", 1)[0]
        item_source = (item.description or "").lower()
        if "canvas" not in item_source:
            continue
        duplicate = next((other for other in items
            if other.id != item.id
            and other.course_id == item.course_id
            and other.due_at.date() == item.due_at.date()
            and (other.description or "Assignment").split(" · ", 1)[0] == item_category
            and "zoom" in (other.description or "").lower()), None)
        if duplicate:
            db.delete(duplicate)
            removed += 1
    return removed


def canvas_category(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in ("optional", "tour", "panel of peers", "cross campus", "lunch", "social", "event")):
        return "Other"
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


def sync_canvas_feed(db: Session, preview: bool = False) -> int:
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
                property_name, value = line.split(":", 1)
                key = property_name.split(";", 1)[0]
                fields[key] = (property_name, value.replace("\\,", ",").replace("\\n", " "))
        if fields.get("SUMMARY") and fields.get("DTSTART"):
            try:
                due = parse_canvas_datetime(*fields["DTSTART"])
            except ValueError:
                continue
                events.append((fields["SUMMARY"][1], due, fields.get("LOCATION", ("LOCATION", ""))[1], fields.get("UID", ("UID", ""))[1]))
    original = {item.id: (item.title, utc_datetime(item.due_at), item.description, item.course_id) for item in db.scalars(select(Assignment)).all()}
    updated = 0
    ignored = 0
    workspace = db.scalar(select(Workspace).limit(1))
    courses = db.scalars(select(Course)).all()
    for title, due, location, event_uid in events:
        if any(fragment in normalized_title(title) for fragment in CANVAS_IGNORED_TITLE_FRAGMENTS):
            continue
        course = resolve_canvas_course(title, courses)
        category = canvas_category(title)
        item = find_canvas_assignment(db, title, course, category, event_uid)
        if item and abs((utc_datetime(item.due_at) - utc_datetime(due)).total_seconds()) > 14 * 86400:
            item = None
        if item:
            if not item.course_id and course:
                item.course_id = course.id
            item.due_at = due
            item.description = f"{(item.description or category + ' · Canvas').split(' · ', 1)[0]} · {location or 'Canvas'}" + (f" · Canvas UID:{event_uid}" if event_uid else "")
            item.priority = "low" if category == "Suggested Reading" else "high" if category == "Assignment" else "medium" if category == "Other" else item.priority
            updated += 1
        elif workspace and course:
            db.add(Assignment(workspace_id=workspace.id, course_id=course.id, title=title, description=f"{category} · {location or 'Canvas'}" + (f" · Canvas UID:{event_uid}" if event_uid else ""), due_at=due, status=AssignmentStatus.not_started, priority="high" if category == "Assignment" else "medium" if category == "Other" else "normal"))
            updated += 1
        else:
            ignored += 1
    if preview:
        db.flush()
        pending_canvas_sync.clear()
        for item in db.scalars(select(Assignment)).all():
            before = original.get(item.id)
            after = (item.title, utc_datetime(item.due_at), item.description, item.course_id)
            if before and after:
                # Canvas may reformat location text between feed requests;
                # only stable assignment fields should create a review update.
                before = (before[0], before[1], (before[2] or '').split(' · ', 1)[0], before[3])
                after = (after[0], after[1], (after[2] or '').split(' · ', 1)[0], after[3])
            if before != after:
                key = str(item.id)
                pending_canvas_sync[key] = {
                    "id": key, "title": item.title, "due_at": item.due_at.isoformat(),
                    "description": item.description or "Assignment · Canvas",
                    "course_id": item.course_id, "action": "Add" if before is None else "Update",
                }
        db.rollback()
        last_canvas_sync.update({"status": "preview", "updated": len(pending_canvas_sync), "ignored": ignored, "at": datetime.now(timezone.utc).isoformat(), "error": "", "message": "Review Canvas changes before approving them."})
        return len(pending_canvas_sync)
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
        statistics_item = db.scalar(select(Assignment).where(Assignment.title.in_(['Canvas PS 3 [MGT 407 E1]', 'PS 3 due (100 pts)'])).limit(1))
        if statistics_item:
            statistics_item.title = 'PS 3 [MGT 407 E1]'
            statistics_item.description = 'Assignment · 9:00 AM · Canvas'
            statistics_item.due_at = datetime(2026, 9, 25)
            db.commit()
        else:
            workspace = db.scalar(select(Workspace).limit(1))
            course = db.scalar(select(Course).where(Course.code == 'MGT 407').limit(1))
            if workspace and course:
                db.add(Assignment(workspace_id=workspace.id, course_id=course.id, title='PS 3 [MGT 407 E1]', description='Assignment · 9:00 AM · Canvas', due_at=datetime(2026, 9, 25), status=AssignmentStatus.not_started, priority='high'))
                db.commit()
        if remove_non_canvas_duplicates(db):
            db.commit()


@app.post("/api/canvas/sync")
def canvas_sync(db: Session = Depends(get_db)) -> dict:
    updated = sync_canvas_feed(db, preview=True)
    if last_canvas_sync["status"] == "preview" and updated == 0:
        last_canvas_sync.update({
            "status": "success",
            "at": datetime.now(timezone.utc).isoformat(),
            "message": "Canvas is up to date. No new changes were found.",
        })
    payload = {**last_canvas_sync, "updated": updated}
    if payload["status"] == "not_connected":
        payload["message"] = "Canvas is not connected yet. Open Canvas or connect your course feed to enable syncing."
    elif payload["status"] == "preview":
        payload["message"] = f"Canvas found {updated} proposed change(s). Review and approve them before adding to your dashboard."
    elif payload["status"] == "success":
        payload["message"] = payload.get("message") or "Canvas sync completed successfully."
    else:
        payload["message"] = "Canvas sync has not been started yet."
    payload["error"] = payload.get("error") or ""
    payload["items"] = list(pending_canvas_sync.values())
    return payload


@app.post("/api/canvas/sync/approve")
def approve_canvas_sync(payload: dict, db: Session = Depends(get_db)) -> dict[str, int | str]:
    approved = {str(value) for value in payload.get("approved", [])}
    applied = 0
    for key in approved:
        proposal = pending_canvas_sync.get(key)
        if not proposal:
            continue
        item = db.get(Assignment, key)
        if item is None:
            workspace = db.scalar(select(Workspace).limit(1))
            item = Assignment(workspace_id=workspace.id, course_id=proposal["course_id"], title=proposal["title"], due_at=datetime.fromisoformat(proposal["due_at"]), description=proposal["description"], status=AssignmentStatus.not_started, priority="high" if proposal["description"].startswith("Assignment") else "medium" if proposal["description"].startswith("Other") else "normal")
            db.add(item)
        else:
            item.due_at = datetime.fromisoformat(proposal["due_at"]); item.description = proposal["description"]
        applied += 1
    db.commit()
    removed = remove_non_canvas_duplicates(db)
    db.commit()
    pending_canvas_sync.clear()
    last_canvas_sync.update({"status": "success", "updated": applied, "at": datetime.now(timezone.utc).isoformat(), "message": f"Canvas sync approved. {applied} change(s) applied; {removed} duplicate(s) removed."})
    return {**last_canvas_sync, "updated": applied}


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


@app.delete("/api/assignments/{assignment_id}")
def delete_assignment(assignment_id: str, db: Session = Depends(get_db)) -> dict:
    item = db.get(Assignment, assignment_id)
    if not item:
        return {"status": "not_found"}
    db.delete(item)
    db.commit()
    return {"status": "deleted", "id": assignment_id}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    restore_canvas_sync_status(db)
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
    try:
        deadline_days = int(request.query_params.get('days', '7'))
    except ValueError:
        deadline_days = 7
    if deadline_days not in {7, 10, 14, 21}:
        deadline_days = 7
    deadline_horizon = local_date + timedelta(days=deadline_days - 1)
    upcoming = [item for item in calendar_items if local_date <= item.due_at.date() <= deadline_horizon and not (item.description or '').startswith('Class ·')]
    deadline_items = [item for item in calendar_items if not (item.description or '').startswith('Class ·') and (item.due_at.replace(tzinfo=timezone.utc) if item.due_at.tzinfo is None else item.due_at) >= now]
    completed_deadlines = sum(item.status == AssignmentStatus.complete for item in deadline_items)
    completion_percent = round((completed_deadlines / len(deadline_items)) * 100) if deadline_items else 0
    agenda_items = [
        item for item in upcoming
        if (item.description or "Assignment").split(" · ", 1)[0]
        in {"Assignment", "Required Reading", "Review Session"}
    ][:4]
    calendar_only = request.query_params.get('view') == 'calendar'
    sync_status = last_canvas_sync["status"]
    sync_label = {"success": "Connected", "not_connected": "Needs attention"}.get(sync_status, "Not synced")
    sync_detail = last_canvas_sync["message"] if sync_status != "success" else f"Last synced {last_canvas_sync['at'].replace('T', ' ')[:16]} UTC"
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "assignments": upcoming,
            "deadline_days": deadline_days,
            "today": now.date(),
            "week_end": (now + timedelta(days=deadline_days)).date(),
            "agenda_label": agenda_label,
            "agenda_start": agenda_start,
            "agenda_end": agenda_end,
            "course_names": course_names,
            "display_due_at": display_due_at,
            "assignment_source": assignment_source,
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
            "canvas_sync_label": sync_label,
            "canvas_sync_detail": sync_detail,
            "canvas_sync_status": sync_status,
        },
    )
