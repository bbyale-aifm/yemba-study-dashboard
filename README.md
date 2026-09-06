# EMBA Study Dashboard

A student-facing dashboard for keeping Yale EMBA coursework, deadlines, readings, and class logistics organized in one place.

This project was built to help a cohort stay on top of assignments, important dates, and study planning without bouncing between multiple tools and scattered notes.

## Why this exists

EMBA students often juggle:
- course calendars
- assigned readings and course materials
- review sessions and weekend class logistics
- deadlines across multiple classes
- personal study planning

This dashboard brings those pieces together into one simple, mobile-friendly workspace.

## Features

- Overview of upcoming assignments and deadlines
- Course material tracking for readings and resources
- Academic calendar view for the term
- Canvas-style sync for course feeds
- Syllabus upload workflow to parse and import course info
- Study assistant for planning the week or weekend
- Mobile-friendly dashboard layout for quick updates on the go

## Demo / current status

This is a working prototype designed for a cohort or study group. It currently uses a local SQLite database and seeded sample data so classmates can run it easily on a laptop or personal machine.

## Project structure

```text
emba-study-dashboard/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── flow.py
│   ├── models.py
│   ├── schemas.py
│   ├── templates/
│   │   └── dashboard.html
│   ├── static/
│   │   └── dashboard.css
│   └── __init__.py
├── data/
│   └── study_flow.json
├── .gitignore
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── emba-dashboard.db
└── .env.example
```

## Tech stack

- FastAPI for the backend
- Jinja templates for the UI
- SQLAlchemy + SQLite for local data storage
- Python for syllabus parsing and timeline logic

## Local setup

1. Clone the repo
2. Create a virtual environment
3. Install dependencies
4. Run the app

```bash
cd emba-study-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

## Main workflows

### Upload a syllabus

You can upload a PDF or Word doc, and the app will try to extract course information and create relevant study items.

### Sync Canvas calendar

The app includes a Canvas calendar sync flow that imports nearby academic items and updates the dashboard view.

### Study assistant

The assistant helps with planning by surfacing the next deadlines and suggesting priorities for the week or weekend.

## Notes for classmates

This project is intentionally simple and easy to use:
- low-friction interface
- easy local setup
- no complicated auth flow required for the prototype
- designed to be shared and adapted by students

## Future ideas

Possible next steps include:
- better authentication for shared use
- persistent course data beyond a local prototype
- richer resource organization
- Google Calendar integration
- deployment to a hosted app for a cohort

## License

This project is intended for educational and cohort-sharing use. If you plan to share or adapt it, please credit the original project and keep the code open for classmates to learn from.

## Contributing

If you want to improve the app, feel free to fork the repo, make your changes, and submit a pull request.
