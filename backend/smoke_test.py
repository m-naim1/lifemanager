"""End-to-end smoke test of the LifeManager API via httpx TestClient.

Uses a temporary database so it never touches real data.
Run from the backend directory:  uv run python smoke_test.py
"""

from __future__ import annotations

import os
import tempfile
from datetime import date

os.environ["LIFEMANAGER_DB_PATH"] = os.path.join(
    tempfile.gettempdir(), "lifemanager_smoke_test.db"
)
if os.path.exists(os.environ["LIFEMANAGER_DB_PATH"]):
    os.remove(os.environ["LIFEMANAGER_DB_PATH"])

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_today_smoke(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True

    today = date.today().isoformat()
    r = client.post("/api/projects", json={"name": "Website", "deadline": today})
    assert r.status_code == 200, r.text
    project = r.json()
    assert project["status"] == "open"

    r = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "Write landing page"},
    )
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["project_id"] == project["id"]
    assert task["done"] is False

    r = client.post(
        "/api/tasks",
        json={
            "title": "Learn Alpine",
            "schedule": {
                "repeat_count": 2,
                "repeat_unit": "week",
                "duration_count": 3,
                "duration_unit": "week",
            },
        },
    )
    assert r.status_code == 200, r.text
    repeating = r.json()
    assert repeating["schedule"]["repeat_count"] == 2
    assert repeating["schedule"]["end_date"] is not None
    assert repeating["schedule"]["duration_unit"] == "week"

    r = client.post(
        "/api/routines",
        json={"name": "Exercise", "frequency_count": 3, "frequency_unit": "day"},
    )
    assert r.status_code == 200, r.text
    routine = r.json()

    r = client.get("/api/home/today")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert any(p["id"] == project["id"] for p in payload["projects_due_today"])
    assert any(t["task"]["id"] == task["id"] for t in payload["tasks"])
    assert any(
        t["task"]["id"] == repeating["id"] and t["progress"]["target"] == 2
        for t in payload["tasks"]
    )
    assert any(
        rt["routine"]["id"] == routine["id"]
        and rt["progress"]["target"] == 3
        for rt in payload["routines"]
    )

    client.post(f"/api/tasks/{task['id']}/complete")
    client.post(f"/api/tasks/{repeating['id']}/complete")
    client.post(f"/api/tasks/{repeating['id']}/complete")
    client.post(f"/api/routines/{routine['id']}/complete")

    stats = client.get("/api/stats/heatmap", params={"type": "tasks"}).json()
    assert stats["total"] >= 3
    stats = client.get("/api/stats/heatmap", params={"type": "routines"}).json()
    assert stats["total"] >= 1

    payload = client.get("/api/home/today").json()
    assert all(t["task"]["id"] != task["id"] for t in payload["tasks"])
    remaining = [
        rt for rt in payload["routines"] if rt["routine"]["id"] == routine["id"]
    ]
    assert remaining and remaining[0]["progress"]["completed"] == 1

    r = client.delete(f"/api/routines/{routine['id']}/completions/latest")
    assert r.status_code == 200, r.text

    for status in ("closed", "open", "archived"):
        r = client.patch(f"/api/projects/{project['id']}", json={"status": status})
        assert r.status_code == 200 and r.json()["status"] == status


def test_task_dates_and_modes(client: TestClient) -> None:
    # Task with explicit start/due date
    r = client.post(
        "/api/tasks",
        json={
            "title": "File taxes",
            "start_date": "2025-01-10",
            "due_date": "2025-01-15",
            "completion_mode": "wait",
        },
    )
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["start_date"] == "2025-01-10"
    assert t["due_date"] == "2025-01-15"
    assert t["completion_mode"] == "wait"

    # Carried one-time task (no schedule, past due date)
    r = client.post(
        "/api/tasks",
        json={
            "title": "Something late",
            "due_date": "2020-01-01",
            "completion_mode": "carry",
        },
    )
    assert r.status_code == 200, r.text
    late = r.json()
    today_payload = client.get("/api/home/today").json()
    late_entry = next(
        (e for e in today_payload["tasks"] if e["task"]["id"] == late["id"]), None
    )
    assert late_entry is not None
    assert late_entry["progress"]["overdue"] is True

    # Waited one-time task (past due date) surfaces under overdue, not tasks
    r = client.post(
        "/api/tasks",
        json={
            "title": "Decide its fate",
            "due_date": "2020-01-01",
            "completion_mode": "wait",
        },
    )
    assert r.status_code == 200, r.text
    waited = r.json()
    today_payload = client.get("/api/home/today").json()
    assert all(e["task"]["id"] != waited["id"] for e in today_payload["tasks"])
    overdue_entry = next(
        (e for e in today_payload["overdue"] if e["task"]["id"] == waited["id"]), None
    )
    assert overdue_entry is not None and overdue_entry["progress"]["overdue"] is True

    # Repeating task with explicit slot_days (Mon=0, Wed=2)
    r = client.post(
        "/api/tasks",
        json={
            "title": "Go gym",
            "schedule": {
                "repeat_count": 2,
                "repeat_unit": "week",
                "slot_days": [0, 2],
                "duration_count": 4,
                "duration_unit": "week",
            },
        },
    )
    assert r.status_code == 200, r.text
    gym = r.json()
    assert gym["schedule"]["slot_days"] == [0, 2]

    # Routine with explicit slot_days
    r = client.post(
        "/api/routines",
        json={
            "name": "Journal",
            "frequency_count": 3,
            "frequency_unit": "week",
            "slot_days": [0, 2, 4],
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["slot_days"] == [0, 2, 4]


def test_notes_and_calendar(client: TestClient) -> None:
    r = client.post("/api/tasks", json={"title": "Read"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    day = date.today().isoformat()

    r = client.put(f"/api/tasks/{tid}/notes/{day}", json={"note": "Ch. 4-6"})
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "Ch. 4-6"

    r = client.get(f"/api/tasks/{tid}/notes")
    assert r.status_code == 200, r.text
    assert any(n["date"] == day and n["note"] == "Ch. 4-6" for n in r.json())

    r = client.put(f"/api/tasks/{tid}/notes/{day}", json={"note": "Ch. 7"})
    assert r.json()["note"] == "Ch. 7"

    r = client.delete(f"/api/tasks/{tid}/notes/{day}")
    assert r.status_code == 200, r.text
    r = client.get(f"/api/tasks/{tid}/notes")
    assert not any(n["date"] == day for n in r.json())

    today = date.today()
    cal = client.get(
        "/api/stats/calendar",
        params={"type": "tasks", "year": today.year, "month": today.month},
    ).json()
    assert cal["year"] == today.year
    assert cal["month"] == today.month
    assert len(cal["days"]) >= 28


def test_goals(client: TestClient) -> None:
    r = client.post(
        "/api/goals",
        json={
            "title": "Colemak typing",
            "description": "Reach a strong average.",
            "kind": "measured",
            "fields": [
                {
                    "name": "WPM",
                    "unit": "WPM",
                    "target_value": 90,
                    "compare_mode": "average",
                },
                {
                    "name": "Accuracy",
                    "unit": "%",
                    "target_value": 97,
                    "compare_mode": "latest",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    goal = r.json()
    assert goal["kind"] == "measured"
    assert len(goal["fields"]) == 2
    goal_id = goal["id"]
    wpm = next(f for f in goal["fields"] if f["name"] == "WPM")
    accuracy = next(f for f in goal["fields"] if f["name"] == "Accuracy")
    assert wpm["compare_mode"] == "average"
    assert goal["charts"][0]["has_data"] is False

    r = client.post(
        f"/api/goals/{goal_id}/entries",
        json={
            "date": "2026-11-01",
            "note": "first session",
            "values": [
                {"field_id": wpm["id"], "value": 80},
                {"field_id": accuracy["id"], "value": 95},
            ],
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/goals/{goal_id}/entries",
        json={
            "date": "2026-11-03",
            "note": "second session",
            "values": [
                {"field_id": wpm["id"], "value": 90},
                {"field_id": accuracy["id"], "value": 98},
            ],
        },
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["record_count"] == 2
    wpm_summary = next(f for f in detail["fields"] if f["id"] == wpm["id"])
    assert wpm_summary["average"] == 85
    assert wpm_summary["latest"] == 90
    assert wpm_summary["best"] == 90
    assert wpm_summary["at_target"] is False  # average 85 < 90
    accuracy_summary = next(f for f in detail["fields"] if f["id"] == accuracy["id"])
    assert accuracy_summary["latest"] == 98
    assert accuracy_summary["at_target"] is True
    assert len(detail["charts"]) == 2
    assert detail["charts"][0]["has_data"] is True
    assert str(wpm_summary["id"]) in str(detail["charts"][0]["field_id"])

    # Update a record and verify the summary follows.
    entry_id = detail["entries"][-1]["id"]
    r = client.patch(
        f"/api/goals/{goal_id}/entries/{entry_id}",
        json={
            "values": [
                {"field_id": wpm["id"], "value": 100},
                {"field_id": accuracy["id"], "value": 98},
            ]
        },
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    wpm_summary = next(f for f in detail["fields"] if f["id"] == wpm["id"])
    assert wpm_summary["average"] == 90
    assert wpm_summary["at_target"] is True

    # Goal editing keeps field ids and updates the target.
    r = client.patch(
        f"/api/goals/{goal_id}",
        json={
            "title": "Colemak typing v2",
            "fields": [
                {
                    "id": wpm["id"],
                    "name": "WPM",
                    "unit": "WPM",
                    "target_value": 85,
                    "compare_mode": "average",
                },
                {
                    "id": accuracy["id"],
                    "name": "Accuracy",
                    "unit": "%",
                    "target_value": 97,
                    "compare_mode": "latest",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Colemak typing v2"
    assert r.json()["achieved"] is True

    # Detail page renders the SVG charts and history while charts are enabled.
    page = client.get(f"/goals/{goal_id}")
    assert page.status_code == 200, page.text[:500]
    assert "Progress over time" in page.text
    assert "<svg" in page.text
    assert "Colemak typing v2" in page.text

    # Delete a record.
    r = client.delete(f"/api/goals/{goal_id}/entries/{entry_id}")
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/goals/{goal_id}").json()
    assert detail["record_count"] == 1

    # Percent goal.
    r = client.post(
        "/api/goals",
        json={"title": "Get strong", "kind": "percent"},
    )
    assert r.status_code == 200, r.text
    percent_goal = r.json()
    assert percent_goal["kind"] == "percent"
    assert percent_goal["fields"][0]["target_value"] == 100
    progress_field = percent_goal["fields"][0]
    r = client.post(
        f"/api/goals/{percent_goal['id']}/entries",
        json={"values": [{"field_id": progress_field["id"], "value": 40}]},
    )
    assert r.status_code == 200, r.text
    percent_detail = r.json()
    assert percent_detail["percent"] == 40
    r = client.post(
        f"/api/goals/{percent_goal['id']}/entries",
        json={"values": [{"field_id": progress_field["id"], "value": 120}]},
    )
    assert r.status_code == 400, r.text

    # Staged goal: ordered checklist with per-stage completion.
    r = client.post(
        "/api/goals",
        json={
            "title": "Learn frontend",
            "kind": "staged",
            "stages": [
                {"title": "HTML"},
                {"title": "CSS"},
                {"title": "JavaScript"},
                {"title": "Tailwind"},
                {"title": "React"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    staged = r.json()
    assert staged["kind"] == "staged"
    assert staged["stage_count"] == 5
    assert staged["stages_done"] == 0
    assert staged["percent"] == 0
    assert [s["title"] for s in staged["stages"]] == [
        "HTML",
        "CSS",
        "JavaScript",
        "Tailwind",
        "React",
    ]
    staged_id = staged["id"]
    html_stage = staged["stages"][0]
    r = client.post(f"/api/goals/{staged_id}/stages/{html_stage['id']}/toggle")
    assert r.status_code == 200, r.text
    staged = r.json()
    assert staged["stages_done"] == 1
    assert staged["percent"] == 20
    assert staged["stages"][0]["done"] is True
    assert staged["stages"][0]["completed_at"] is not None
    r = client.post(f"/api/goals/{staged_id}/stages/{html_stage['id']}/toggle")
    assert r.status_code == 200, r.text
    assert r.json()["stages_done"] == 0

    r = client.post(f"/api/goals/{staged_id}/stages", json={"title": "TypeScript"})
    assert r.status_code == 200, r.text
    assert r.json()["stage_count"] == 6
    new_stage = r.json()["stages"][-1]
    r = client.patch(
        f"/api/goals/{staged_id}/stages/{new_stage['id']}",
        json={"done": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["stages_done"] == 1
    r = client.delete(f"/api/goals/{staged_id}/stages/{new_stage['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["stage_count"] == 5

    # Full goal update replaces/renames stages while keeping ids and state.
    current = client.get(f"/api/goals/{staged_id}").json()
    stage_payload = [
        {"id": s["id"], "title": s["title"], "done": s["done"]}
        for s in current["stages"]
    ]
    stage_payload[0]["title"] = "HTML semantics"
    stage_payload[0]["done"] = True
    r = client.patch(
        f"/api/goals/{staged_id}",
        json={"stages": stage_payload},
    )
    assert r.status_code == 200, r.text
    assert r.json()["stages"][0]["title"] == "HTML semantics"
    assert r.json()["stages"][0]["done"] is True
    assert r.json()["stages_done"] == 1

    # Web edit form updates the same stages.
    page = client.post(
        f"/goals/{staged_id}",
        data={
            "title": "Learn frontend",
            "description": "A staged path.",
            "show_chart": "1",
            "stage_id": [str(s["id"]) for s in stage_payload],
            "stage_title": [s["title"] for s in stage_payload],
            "stage_done": ["1" if s["done"] else "0" for s in stage_payload],
        },
        follow_redirects=False,
    )
    assert page.status_code == 303, page.text[:500]

    # Web toggle and inline add-stage flow.
    page = client.post(
        f"/goals/{staged_id}/stages/{staged['stages'][1]['id']}/toggle",
        headers={"HX-Request": "true"},
    )
    assert page.status_code == 200, page.text[:500]
    assert "CSS" in page.text
    page = client.post(
        f"/goals/{staged_id}/stages",
        data={"title": "Deploy a project"},
        headers={"HX-Request": "true"},
    )
    assert page.status_code == 200, page.text[:500]
    assert "Deploy a project" in page.text
    page = client.get(f"/goals/{staged_id}")
    assert page.status_code == 200, page.text[:500]
    assert "Learn frontend" in page.text
    assert "Checklist" in page.text
    assert "Deploy a project" in page.text

    # Variable-chart toggle and archive.
    r = client.patch(f"/api/goals/{goal_id}", json={"show_chart": False})
    assert r.status_code == 200, r.text
    assert r.json()["charts"] == []
    r = client.patch(f"/api/goals/{goal_id}", json={"status": "archived"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"

    # SSR pages render with the authenticated request.
    for path in ("/goals", f"/goals/{goal_id}", "/goals/new", f"/goals/{goal_id}/edit"):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text[:500])
    assert "Colemak typing" in client.get("/goals").text
    assert "Get strong" in client.get("/").text

    # Web form create/update/entry flows.
    page = client.post(
        "/goals",
        data={"title": "Read 30 books", "kind": "percent", "show_chart": "1"},
    )
    assert page.status_code == 200, page.text[:500]
    assert "Read 30 books" in page.text
    read_goal = next(
        g for g in client.get("/api/goals").json() if g["title"] == "Read 30 books"
    )
    read_field = read_goal["fields"][0]
    page = client.post(
        f"/goals/{read_goal['id']}/entries",
        data={"date": date.today().isoformat(), "field_id": read_field["id"], "value": "25", "note": "on track"},
        headers={"HX-Request": "true"},
    )
    assert page.status_code == 200, page.text[:500]
    assert "25%" in page.text
    updated = client.get(f"/api/goals/{read_goal['id']}").json()
    assert updated["percent"] == 25

    # Delete both goals (cascade).
    for gid in (goal_id, percent_goal["id"], read_goal["id"], staged_id):
        assert client.delete(f"/api/goals/{gid}").status_code == 200
    assert client.get(f"/api/goals/{goal_id}").status_code == 404


def test_admin(client: TestClient) -> None:
    me = client.get("/api/auth/me").json()
    assert me["is_admin"] is True

    overview = client.get("/api/admin/overview")
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["totals"]["users"] >= 1
    assert data["totals"]["admin_users"] >= 1
    assert any(u["email"] == me["email"] for u in data["users"])

    page = client.get("/admin")
    assert page.status_code == 200, page.text[:500]
    assert "Admin activity" in page.text
    assert "New accounts" in page.text
    assert "Users" in page.text

    # Create a member through the admin API.
    created = client.post(
        "/api/admin/users",
        json={
            "email": "managed@test.com",
            "password": "managed123",
            "is_admin": False,
            "is_active": True,
        },
    )
    assert created.status_code == 200, created.text
    managed = created.json()
    assert managed["email"] == "managed@test.com"
    assert managed["project_count"] == 0
    managed_id = managed["id"]

    # The new account can log in with the temporary password.
    login = client.post(
        "/api/auth/login",
        json={"email": "managed@test.com", "password": "managed123"},
    )
    assert login.status_code == 200, login.text

    # Admin user detail and modal pages render.
    detail = client.get(f"/api/admin/users/{managed_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["account"]["email"] == "managed@test.com"
    for path in (
        f"/admin/users/{managed_id}",
        "/admin/users/new",
        f"/admin/users/{managed_id}/edit",
        f"/admin/users/{managed_id}/reset-password",
    ):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text[:400])

    # Promote and rename.
    updated = client.patch(
        f"/api/admin/users/{managed_id}",
        json={"email": "managed2@test.com", "is_admin": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "managed2@test.com"
    assert updated.json()["is_admin"] == 1

    # Reset password and verify it.
    reset = client.patch(
        f"/api/admin/users/{managed_id}",
        json={"password": "newpassword123"},
    )
    assert reset.status_code == 200, reset.text
    login = client.post(
        "/api/auth/login",
        json={"email": "managed2@test.com", "password": "newpassword123"},
    )
    assert login.status_code == 200, login.text

    # Audit trail recorded the actions.
    audit = client.get("/api/admin/audit")
    assert audit.status_code == 200, audit.text
    actions = {item["action"] for item in audit.json()}
    assert {"user.create", "user.update", "user.reset_password"} <= actions

    # Safety rails: cannot disable or delete yourself.
    assert (
        client.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code
        == 400
    )
    assert client.delete(f"/api/admin/users/{me['id']}").status_code == 400

    # Delete the managed account and confirm it is gone.
    deleted = client.delete(f"/api/admin/users/{managed_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/admin/users/{managed_id}").status_code == 404


if __name__ == "__main__":
    with TestClient(app) as client:
        # Authenticate (all /api/* except health + login/register require JWT).
        reg = client.post(
            "/api/auth/register",
            json={"email": "smoke@test.com", "password": "password123"},
        )
        assert reg.status_code == 200, reg.text
        client.headers.update(
            {"Authorization": f"Bearer {reg.json()['token']}"}
        )
        test_today_smoke(client)
        test_task_dates_and_modes(client)
        test_notes_and_calendar(client)
        test_goals(client)
        test_admin(client)
        print("ALL SMOKE TESTS PASSED")