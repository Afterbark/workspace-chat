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
    if appmod.limiter:
        appmod.limiter.enabled = False  # tests register many users from one address
    with appmod.app.app_context():
        appmod.db.drop_all()
        appmod.db.create_all()
    # in-memory presence and call state would otherwise leak between tests (user ids are reused)
    for state in (appmod.online_users, appmod.user_status, appmod.active_calls, appmod.user_call):
        state.clear()
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


# ---------- access control over websockets ----------

def _socket_for(username):
    """Register `username` with its own HTTP client and return a logged-in Socket.IO test client."""
    http = appmod.app.test_client()
    _register(http, username)
    return appmod.socketio.test_client(appmod.app, flask_test_client=http)


def _make_group(name, *usernames):
    with appmod.app.app_context():
        users = [appmod.User.query.filter_by(username=u).first() for u in usernames]
        g = appmod.ChatGroup(name=name, owner_id=users[0].id)
        for u in users:
            g.members.append(u)
        appmod.db.session.add(g)
        appmod.db.session.commit()
        return g.id


def test_anonymous_socket_rejected(client):
    sio = appmod.socketio.test_client(appmod.app, flask_test_client=appmod.app.test_client())
    assert not sio.is_connected()


def test_non_member_cannot_read_group(client):
    member = _socket_for("ivy")
    outsider = _socket_for("jack")
    gid = _make_group("secret", "ivy")
    member.emit("send_message", {"type": "group", "id": gid, "message": "private plans"})

    outsider.get_received()
    outsider.emit("join_chat", {"type": "group", "id": gid})
    assert not [e for e in outsider.get_received() if e["name"] == "load_history"]

    member.get_received()
    member.emit("join_chat", {"type": "group", "id": gid})
    history = [e for e in member.get_received() if e["name"] == "load_history"]
    assert history and history[0]["args"][0]["messages"][0]["content"] == "private plans"


def test_non_member_cannot_post_to_group(client):
    _socket_for("kim")
    outsider = _socket_for("lou")
    gid = _make_group("closed", "kim")
    outsider.emit("send_message", {"type": "group", "id": gid, "message": "spam"})
    with appmod.app.app_context():
        assert appmod.Message.query.filter_by(group_id=gid, content="spam").count() == 0


def test_blank_message_ignored(client):
    a = _socket_for("max")
    _socket_for("ned")
    with appmod.app.app_context():
        ned_id = appmod.User.query.filter_by(username="ned").first().id
    a.emit("send_message", {"type": "dm", "id": ned_id, "message": "   "})
    with appmod.app.app_context():
        assert appmod.Message.query.filter_by(receiver_id=ned_id).count() == 0


def test_username_with_markup_rejected(client):
    _register(client, "<img src=x onerror=alert(1)>")
    with appmod.app.app_context():
        assert appmod.User.query.filter(appmod.User.username.like("<img%")).count() == 0


def test_timestamps_are_utc():
    from datetime import datetime, timezone
    drift = abs((appmod.utcnow() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
    assert drift < 5


# ---------- chat list previews ----------

def test_chat_previews_pick_newest_message_per_chat(client):
    with appmod.app.app_context():
        db, User, Message = appmod.db, appmod.User, appmod.Message
        a, b, c = (User(username=n, password="x") for n in ("pia", "quin", "rex"))
        db.session.add_all([a, b, c])
        db.session.commit()
        db.session.add_all([
            Message(sender_id=a.id, receiver_id=b.id, content="first"),
            Message(sender_id=b.id, receiver_id=a.id, content="  newest\n  reply "),
            Message(sender_id=c.id, receiver_id=a.id, file_url="/x.jpg", file_type="image", file_name="x.jpg"),
            Message(sender_id=a.id, receiver_id=a.id, content="note to self", is_deleted=True),
            Message(sender_id=b.id, receiver_id=c.id, content="not mine"),
        ])
        db.session.commit()
        gid = _make_group("crew", "pia", "quin")
        db.session.add(Message(sender_id=b.id, group_id=gid, content="group hello"))
        db.session.commit()

        p = appmod.chat_previews(a.id, [gid])
        assert p[f"dm:{b.id}"]["text"] == "newest reply"
        assert p[f"dm:{b.id}"]["sender_id"] == b.id
        assert p[f"dm:{c.id}"]["text"] == "📷 Photo"
        assert p[f"dm:{a.id}"]["text"] == "Message deleted"
        assert p[f"group:{gid}"]["text"] == "group hello"
        assert p[f"group:{gid}"]["sender"] == "quin"
        assert len(p) == 4  # quin -> rex is someone else's chat


def test_dm_packet_names_the_receiver(client):
    sender = _socket_for("sol")
    _socket_for("tam")
    with appmod.app.app_context():
        tam_id = appmod.User.query.filter_by(username="tam").first().id
    sender.emit("join_chat", {"type": "dm", "id": tam_id})
    sender.get_received()
    sender.emit("send_message", {"type": "dm", "id": tam_id, "message": "hi"})
    packets = [e["args"][0] for e in sender.get_received() if e["name"] == "receive_message"]
    assert packets and packets[0]["receiver_id"] == tam_id


def test_typing_reaches_recipient_who_has_not_opened_the_chat(client):
    sender = _socket_for("uma")
    other = _socket_for("val")
    with appmod.app.app_context():
        val_id = appmod.User.query.filter_by(username="val").first().id
    other.emit("register_user")   # what the page does on load: join the personal room, nothing else
    other.get_received()
    sender.emit("typing", {"type": "dm", "id": val_id})
    typing = [e for e in other.get_received() if e["name"] == "user_typing"]
    assert typing and typing[0]["args"][0]["username"] == "uma"


def test_only_site_admins_get_the_approval_inbox(client, monkeypatch):
    larry_http, mallory_http = appmod.app.test_client(), appmod.app.test_client()
    _register(larry_http, "Larry")
    _register(mallory_http, "mallory")
    monkeypatch.setattr(appmod, "_ADMIN_USERNAMES", {"larry"})
    # the bell shows on the desktop rail and in the phone header, for the admin only
    assert larry_http.get("/dashboard").get_data(as_text=True).count('aria-label="Approval requests"') == 2
    assert 'aria-label="Approval requests"' not in mallory_http.get("/dashboard").get_data(as_text=True)

    with appmod.app.app_context():
        pending = appmod.User(username="newbie", password="x", approved=False)
        appmod.db.session.add(pending)
        appmod.db.session.commit()
        req = appmod.ApprovalRequest(kind="signup", user_id=pending.id, username="newbie")
        appmod.db.session.add(req)
        appmod.db.session.commit()
        req_id = req.id

    mallory = appmod.socketio.test_client(appmod.app, flask_test_client=mallory_http)
    mallory.emit("approve_request", {"id": req_id})
    with appmod.app.app_context():
        assert not appmod.User.query.filter_by(username="newbie").first().approved

    larry = appmod.socketio.test_client(appmod.app, flask_test_client=larry_http)
    larry.emit("approve_request", {"id": req_id})
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username="newbie").first().approved
        assert appmod.ApprovalRequest.query.count() == 0


# ---------- scheduled messages ----------

def _online(username):
    """A logged-in socket that has joined its personal room, like an open dashboard tab."""
    s = _socket_for(username)
    s.emit("register_user")
    s.get_received()
    return s


def _uid(username):
    with appmod.app.app_context():
        return appmod.User.query.filter_by(username=username).first().id


def test_schedule_list_cancel_and_send_now(client):
    from datetime import datetime, timedelta, timezone
    amy, ben = _online("amy"), _online("ben")
    ben_id = _uid("ben")
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    amy.emit("schedule_message", {"type": "dm", "id": ben_id, "message": "see you at 9", "send_at": later})
    got = amy.get_received()
    assert any(e["name"] == "scheduled_ack" and e["args"][0]["ok"] for e in got)
    items = [e for e in got if e["name"] == "scheduled_list"][-1]["args"][0]["items"]
    assert [i["content"] for i in items] == ["see you at 9"]
    sid = items[0]["id"]

    # someone else can't cancel or send it
    ben.emit("cancel_scheduled", {"id": sid})
    ben.emit("send_scheduled_now", {"id": sid})
    with appmod.app.app_context():
        assert appmod.ScheduledMessage.query.get(sid).sent is False

    # send now delivers it once and empties the list
    amy.emit("send_scheduled_now", {"id": sid})
    amy.emit("send_scheduled_now", {"id": sid})
    with appmod.app.app_context():
        assert appmod.Message.query.filter_by(content="see you at 9").count() == 1
    assert any(e["name"] == "receive_message" and e["args"][0]["message"] == "see you at 9" for e in ben.get_received())

    # a second one can be cancelled
    amy.emit("schedule_message", {"type": "dm", "id": ben_id, "message": "never mind", "send_at": later})
    sid2 = [e for e in amy.get_received() if e["name"] == "scheduled_list"][-1]["args"][0]["items"][0]["id"]
    amy.emit("cancel_scheduled", {"id": sid2})
    assert [e for e in amy.get_received() if e["name"] == "scheduled_list"][-1]["args"][0]["items"] == []


def test_schedule_rejects_past_times(client):
    from datetime import datetime, timedelta, timezone
    cal = _online("cal")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cal.emit("schedule_message", {"type": "dm", "id": _uid("cal"), "message": "too late", "send_at": past})
    acks = [e["args"][0] for e in cal.get_received() if e["name"] == "scheduled_ack"]
    assert acks and acks[0]["ok"] is False
    with appmod.app.app_context():
        assert appmod.ScheduledMessage.query.count() == 0


# ---------- shared media ----------

def test_shared_media_sorts_photos_files_and_links(client):
    dee, outsider = _online("dee"), _online("eve")
    gid = _make_group("crew", "dee")
    with appmod.app.app_context():
        dee_id = _uid("dee")
        for kw in ({"file_url": "/p.jpg", "file_type": "image", "file_name": "p.jpg"},
                   {"file_url": "/g", "file_type": "image", "file_name": "gif.gif"},
                   {"file_url": "/v.webm", "file_type": "video", "file_name": "voice_123.webm"},
                   {"file_url": "/r.pdf", "file_type": "document", "file_name": "report.pdf"},
                   {"content": "look https://example.com/a, and again https://example.com/a"},
                   {"content": "no link here"}):
            appmod.db.session.add(appmod.Message(sender_id=dee_id, group_id=gid, **kw))
        appmod.db.session.commit()

    outsider.emit("get_shared_media", {"type": "group", "id": gid})
    assert not [e for e in outsider.get_received() if e["name"] == "shared_media"]

    dee.emit("get_shared_media", {"type": "group", "id": gid})
    d = [e for e in dee.get_received() if e["name"] == "shared_media"][0]["args"][0]
    assert sorted(m["kind"] for m in d["media"]) == ["gif", "image"]
    assert sorted(f["kind"] for f in d["files"]) == ["document", "voice"]
    assert [l["url"] for l in d["links"]] == ["https://example.com/a"]   # each link once


# ---------- calls ----------

def test_call_rings_connects_relays_and_logs(client):
    fay, gus, hal = _online("fay"), _online("gus"), _online("hal")
    gus_id = _uid("gus")
    resp = fay.emit("call_invite", {"to": gus_id, "kind": "video"}, callback=True)
    assert resp["ok"]
    call_id = resp["call_id"]
    ring = [e for e in gus.get_received() if e["name"] == "call_incoming"]
    assert ring and ring[0]["args"][0]["from_name"] == "fay" and ring[0]["args"][0]["kind"] == "video"

    # only the person being called can pick up, and nobody else can listen in
    assert hal.emit("call_accept", {"call_id": call_id}, callback=True)["ok"] is False
    assert gus.emit("call_accept", {"call_id": call_id}, callback=True)["ok"] is True
    assert any(e["name"] == "call_accepted" for e in fay.get_received())
    fay.emit("call_signal", {"call_id": call_id, "data": {"type": "offer", "sdp": "v=0"}})
    hal.emit("call_signal", {"call_id": call_id, "data": {"type": "offer", "sdp": "evil"}})
    relayed = [e["args"][0]["data"] for e in gus.get_received() if e["name"] == "call_signal"]
    assert relayed == [{"type": "offer", "sdp": "v=0"}]

    # a third person gets "busy" while they talk
    assert hal.emit("call_invite", {"to": gus_id, "kind": "audio"}, callback=True)["reason"] == "busy"

    gus.emit("call_hangup", {"call_id": call_id})
    assert any(e["name"] == "call_ended" for e in fay.get_received())
    with appmod.app.app_context():
        lines = [m.content for m in appmod.Message.query.filter_by(is_system=True).all()]
    assert any(t.startswith("🎥 Video call · ") for t in lines)
    assert call_id not in appmod.active_calls and gus_id not in appmod.user_call


def test_call_to_offline_or_blocked_user(client):
    ida = _online("ida")
    _socket_for("jon")          # signed up, but no dashboard open
    jon_id = _uid("jon")
    assert ida.emit("call_invite", {"to": jon_id, "kind": "audio"}, callback=True)["reason"] == "offline"
    with appmod.app.app_context():
        assert appmod.Message.query.filter_by(is_system=True, content="📞 Missed voice call from ida").count() == 1
        appmod.db.session.add(appmod.BlockedUser(blocker_id=jon_id, blocked_id=_uid("ida")))
        appmod.db.session.commit()
    assert ida.emit("call_invite", {"to": jon_id, "kind": "audio"}, callback=True)["reason"] == "blocked"


def test_unanswered_call_ends_as_missed(client, monkeypatch):
    import time
    monkeypatch.setattr(appmod, "CALL_RING_SECONDS", 0.2)
    kai, lea = _online("kai"), _online("lea")
    call_id = kai.emit("call_invite", {"to": _uid("lea"), "kind": "audio"}, callback=True)["call_id"]
    time.sleep(0.6)
    ended = [e["args"][0] for e in lea.get_received() if e["name"] == "call_ended"]
    assert ended and ended[0] == {"call_id": call_id, "reason": "no_answer"}
