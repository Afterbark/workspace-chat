"""
Smoke + unit tests for Workspace Chat.

Run locally:
    pip install pytest
    pytest -q

These configure a throwaway SQLite database, so they never touch your
production Postgres data. They cover auth, access control, and the pure
helper functions (URL extraction, SSRF guard, mention parsing).
"""
import os
import tempfile
import importlib
import pytest

# Point the app at a temporary SQLite DB BEFORE importing it.
_TMPDB = os.path.join(tempfile.gettempdir(), "wc_test.db")
if os.path.exists(_TMPDB):
    os.remove(_TMPDB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMPDB
os.environ["SECRET_KEY"] = "test-secret"

import app as appmod  # noqa: E402


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    with appmod.app.app_context():
        appmod.db.drop_all()
        appmod.db.create_all()
    with appmod.app.test_client() as c:
        yield c


def _register(client, username, password="Password1"):
    return client.post("/register", data={"username": username, "password": password},
                       follow_redirects=False)


# ---------- auth ----------

def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code in (301, 302)
    assert "/" in resp.headers.get("Location", "")


def test_register_and_login(client):
    resp = _register(client, "alice")
    assert resp.status_code in (301, 302)  # redirected to dashboard
    # logged in now -> dashboard loads
    assert client.get("/dashboard").status_code == 200


def test_weak_password_rejected(client):
    resp = client.post("/register", data={"username": "bob", "password": "weak"},
                       follow_redirects=True)
    # stays on register page; user not created
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username="bob").first() is None


def test_duplicate_username_rejected(client):
    _register(client, "carol")
    client.get("/logout")
    resp = client.post("/register", data={"username": "carol", "password": "Password1"},
                       follow_redirects=True)
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username="carol").count() == 1


def test_wrong_password(client):
    _register(client, "dave")
    client.get("/logout")
    resp = client.post("/", data={"username": "dave", "password": "WrongPass1"},
                      follow_redirects=True)
    assert b"Invalid username or password" in resp.data


def test_export_requires_login(client):
    resp = client.get("/export_chat?type=dm&id=1")
    assert resp.status_code in (301, 302)


# ---------- pure helpers ----------

def test_extract_first_url():
    assert appmod.extract_first_url("see https://example.com/page now") == "https://example.com/page"
    assert appmod.extract_first_url("no links here") is None


def test_is_safe_url_blocks_private():
    assert appmod.is_safe_url("http://127.0.0.1/") is False
    assert appmod.is_safe_url("ftp://example.com") is False
    assert appmod.is_safe_url("http://169.254.169.254/") is False  # cloud metadata


def test_mentions(client):
    with appmod.app.app_context():
        u1 = appmod.User(username="eve", password="x")
        u2 = appmod.User(username="frank", password="x")
        appmod.db.session.add_all([u1, u2])
        appmod.db.session.commit()
        g = appmod.ChatGroup(name="team", owner_id=u1.id)
        g.members.append(u1)
        g.members.append(u2)
        appmod.db.session.add(g)
        appmod.db.session.commit()
        ids = appmod.extract_mentions("hello @frank and @nobody", g.id)
        assert u2.id in ids
        assert len(ids) == 1


def test_group_admin_helper(client):
    with appmod.app.app_context():
        owner = appmod.User(username="gina", password="x")
        member = appmod.User(username="hank", password="x")
        appmod.db.session.add_all([owner, member])
        appmod.db.session.commit()
        g = appmod.ChatGroup(name="proj", owner_id=owner.id)
        g.members.append(owner)
        g.members.append(member)
        appmod.db.session.add(g)
        appmod.db.session.commit()
        assert appmod.is_group_admin(g, owner) is True
        assert appmod.is_group_admin(g, member) is False
