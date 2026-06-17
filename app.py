from gevent import monkey
monkey.patch_all()
import os
import re
import uuid
import json
import ipaddress
import socket as _socket
import mimetypes
from urllib.parse import urlparse
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, text, and_, or_
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit, join_room

# Optional Cloudinary support (persistent file storage). Falls back to local disk.
try:
    import cloudinary
    import cloudinary.uploader
    _CLOUDINARY_AVAILABLE = True
except Exception:
    _CLOUDINARY_AVAILABLE = False

# Optional Web Push (VAPID). Degrades gracefully if missing/unconfigured.
try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid01
    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False

# Optional HTTP client used for link-preview fetching.
try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except Exception:
    _REQUESTS_AVAILABLE = False

app = Flask(__name__)

# Fallback to dev key if SECRET_KEY environment variable isn't set
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'my_super_secret_key')

# Capture cloud PostgreSQL database URL, fallback to local SQLite for offline dev
db_url = os.environ.get('DATABASE_URL', 'sqlite:///chat.db')

# Convert old 'postgres://' dialect format to SQLAlchemy compatible 'postgresql://'
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url

# --- FILE UPLOAD SETTINGS ---
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['CHAT_UPLOAD_FOLDER'] = 'static/chat_uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB to handle MP4s
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['CHAT_UPLOAD_FOLDER'], exist_ok=True)

# --- CLOUDINARY ---
# Configured automatically from the CLOUDINARY_URL env var, or from the three
# CLOUDINARY_* vars. If none are set we transparently fall back to local disk.
USE_CLOUDINARY = False
if _CLOUDINARY_AVAILABLE:
    if os.environ.get('CLOUDINARY_URL'):
        cloudinary.config(secure=True)
        USE_CLOUDINARY = True
    elif os.environ.get('CLOUDINARY_CLOUD_NAME'):
        cloudinary.config(
            cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
            api_key=os.environ.get('CLOUDINARY_API_KEY'),
            api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
            secure=True,
        )
        USE_CLOUDINARY = True

# --- WEB PUSH (VAPID) keys ---
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_CLAIM = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@example.com')
_vapid = None
if _PUSH_AVAILABLE and VAPID_PRIVATE_KEY:
    try:
        _vapid = Vapid01.from_raw(VAPID_PRIVATE_KEY.encode())
    except Exception:
        _vapid = None

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- RATE LIMITING (in-memory; fine for a single dyno) ---
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")
except Exception:
    limiter = None

def rate_limit(spec, methods=None):
    """Decorator that applies a Flask-Limiter limit if available, else no-op."""
    def deco(f):
        if not limiter:
            return f
        return limiter.limit(spec, methods=methods)(f)
    return deco

# --- ERROR MONITORING (optional, only if SENTRY_DSN is set) ---
if os.environ.get('SENTRY_DSN'):
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(dsn=os.environ['SENTRY_DSN'],
                        integrations=[FlaskIntegration()], traces_sample_rate=0.1)
    except Exception:
        pass

# How many messages to load per "page" when opening a chat / loading older history.
PAGE_SIZE = 30

# In-memory presence tracking: user_id -> number of active socket connections.
online_users = {}

group_members = db.Table('group_members',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('chat_group.id'), primary_key=True)
)

group_admins = db.Table('group_admins',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('chat_group.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    profile_pic = db.Column(db.String(150), default='default')
    last_seen = db.Column(db.DateTime, nullable=True)
    groups = db.relationship('ChatGroup', secondary=group_members, backref=db.backref('members', lazy='dynamic'))
    admin_groups = db.relationship('ChatGroup', secondary=group_admins, backref=db.backref('admins', lazy='dynamic'))

class ChatGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    photo = db.Column(db.String(250), default='default')
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    group_id = db.Column(db.Integer, db.ForeignKey('chat_group.id'), nullable=True)
    content = db.Column(db.Text, nullable=True)
    file_url = db.Column(db.String(250), nullable=True)
    file_type = db.Column(db.String(50), nullable=True)
    file_name = db.Column(db.String(150), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    edited = db.Column(db.Boolean, default=False)
    is_deleted = db.Column(db.Boolean, default=False)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=True)
    pinned = db.Column(db.Boolean, default=False)
    forwarded = db.Column(db.Boolean, default=False)

# Tracks which user has read which message (powers "Seen" / unread counts).
class MessageRead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='uq_message_user_read'),)

# Emoji reactions on messages.
class MessageReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    emoji = db.Column(db.String(16), nullable=False)
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_message_user_emoji'),)

# Browser push-notification subscriptions (Web Push / VAPID).
class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    endpoint = db.Column(db.String(500), unique=True, nullable=False)
    data = db.Column(db.Text, nullable=False)  # full subscription JSON

# Cached Open Graph link previews, keyed by URL.
class LinkPreview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(700), unique=True, nullable=False, index=True)
    title = db.Column(db.String(400))
    description = db.Column(db.String(700))
    image = db.Column(db.String(700))
    ok = db.Column(db.Boolean, default=False)
    fetched_at = db.Column(db.DateTime, default=datetime.now)

# Per-user muted conversations (suppresses notifications, not delivery).
class MutedChat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    chat_type = db.Column(db.String(10), nullable=False)
    chat_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'chat_type', 'chat_id', name='uq_mute'),)

# Per-user archived conversations (hidden from the main list).
class ArchivedChat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    chat_type = db.Column(db.String(10), nullable=False)
    chat_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'chat_type', 'chat_id', name='uq_archive'),)

# Blocked users (blocker hides + can't receive DMs from blocked).
class BlockedUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint('blocker_id', 'blocked_id', name='uq_block'),)

# User reports for moderation review.
class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reported_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    message_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.now)

# Messages scheduled to be sent at a future time.
class ScheduledMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    chat_type = db.Column(db.String(10), nullable=False)
    chat_id = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    send_at = db.Column(db.DateTime, nullable=False, index=True)
    sent = db.Column(db.Boolean, default=False, index=True)


def safe_auto_migrate():
    """Create missing tables and add new columns without destroying data.

    create_all() makes new tables (message_read, message_reaction) but never
    alters existing ones, so we add the new Message columns by hand. Both
    SQLite and Postgres support 'ALTER TABLE ... ADD COLUMN'.
    """
    with app.app_context():
        db.create_all()
        insp = inspect(db.engine)
        try:
            cols = {c['name'] for c in insp.get_columns('message')}
        except Exception:
            return
        to_add = []
        if 'edited' not in cols:
            to_add.append("ALTER TABLE message ADD COLUMN edited BOOLEAN DEFAULT FALSE")
        if 'is_deleted' not in cols:
            to_add.append("ALTER TABLE message ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE")
        if 'reply_to_id' not in cols:
            to_add.append("ALTER TABLE message ADD COLUMN reply_to_id INTEGER")
        if 'pinned' not in cols:
            to_add.append("ALTER TABLE message ADD COLUMN pinned BOOLEAN DEFAULT FALSE")
        if 'forwarded' not in cols:
            to_add.append("ALTER TABLE message ADD COLUMN forwarded BOOLEAN DEFAULT FALSE")
        try:
            gcols = {c['name'] for c in insp.get_columns('chat_group')}
            if 'photo' not in gcols:
                to_add.append("ALTER TABLE chat_group ADD COLUMN photo VARCHAR(250)")
            if 'owner_id' not in gcols:
                to_add.append("ALTER TABLE chat_group ADD COLUMN owner_id INTEGER")
        except Exception:
            pass
        try:
            ucols = {c['name'] for c in insp.get_columns('user')}
            if 'last_seen' not in ucols:
                to_add.append('ALTER TABLE "user" ADD COLUMN last_seen TIMESTAMP')
        except Exception:
            pass
        for stmt in to_add:
            try:
                db.session.execute(text(stmt))
            except Exception:
                db.session.rollback()
        if to_add:
            db.session.commit()


# ---------- HELPERS ----------

def dm_room(a, b):
    return f"dm_{min(a, b)}_{max(a, b)}"

def room_for_message(m):
    if m.group_id:
        return f"group_{m.group_id}"
    return dm_room(m.sender_id, m.receiver_id)

def base_chat_query(chat_type, chat_id, me_id):
    """Return a query for all messages in the given chat, ordered oldest->newest."""
    if chat_type == 'dm':
        q = Message.query.filter(
            ((Message.sender_id == me_id) & (Message.receiver_id == chat_id)) |
            ((Message.sender_id == chat_id) & (Message.receiver_id == me_id))
        )
    else:
        q = Message.query.filter_by(group_id=chat_id)
    return q.order_by(Message.id)

def get_reactions(message_id):
    """Return [{'emoji': '👍', 'user_ids': [..]}] for a message."""
    rows = MessageReaction.query.filter_by(message_id=message_id).all()
    grouped = {}
    for r in rows:
        grouped.setdefault(r.emoji, []).append(r.user_id)
    return [{'emoji': e, 'user_ids': ids} for e, ids in grouped.items()]

def reply_preview(reply_to_id):
    if not reply_to_id:
        return None
    m = Message.query.get(reply_to_id)
    if not m:
        return None
    sender = User.query.get(m.sender_id)
    if m.is_deleted:
        snippet = 'Deleted message'
    elif m.content:
        snippet = m.content[:120]
    elif m.file_type:
        snippet = f'[{m.file_type}]'
    else:
        snippet = ''
    return {'msg_id': m.id, 'sender': sender.username if sender else '?', 'snippet': snippet}

def serialize_message(m):
    return {
        'msg_id': m.id,
        'sender': User.query.get(m.sender_id).username,
        'sender_id': m.sender_id,
        'content': '' if m.is_deleted else m.content,
        'file_url': None if m.is_deleted else m.file_url,
        'file_type': None if m.is_deleted else m.file_type,
        'file_name': None if m.is_deleted else m.file_name,
        'timestamp': m.timestamp.strftime('%I:%M %p'),
        'ts_iso': m.timestamp.isoformat(),
        'edited': bool(m.edited),
        'is_deleted': bool(m.is_deleted),
        'reply_to': reply_preview(m.reply_to_id),
        'reactions': get_reactions(m.id),
        'pinned': bool(m.pinned),
        'forwarded': bool(m.forwarded),
        'preview': None if m.is_deleted else get_content_preview(m.content),
    }

def serialize_messages(msgs):
    """Batch-serialize a list of messages with a fixed number of queries
    (avoids the N+1 problem of calling serialize_message per row)."""
    if not msgs:
        return []
    ids = [m.id for m in msgs]
    sender_ids = {m.sender_id for m in msgs}
    reply_ids = {m.reply_to_id for m in msgs if m.reply_to_id}

    reacts = MessageReaction.query.filter(MessageReaction.message_id.in_(ids)).all()
    react_map = {}
    for r in reacts:
        react_map.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)

    reply_msgs = {rm.id: rm for rm in Message.query.filter(Message.id.in_(reply_ids)).all()} if reply_ids else {}
    for rm in reply_msgs.values():
        sender_ids.add(rm.sender_id)

    umap = {u.id: u for u in User.query.filter(User.id.in_(sender_ids)).all()} if sender_ids else {}

    url_map = {}
    for m in msgs:
        if not m.is_deleted and m.content:
            u = extract_first_url(m.content)
            if u:
                url_map[m.id] = u
    urls = set(url_map.values())
    lp_map = {lp.url: lp for lp in LinkPreview.query.filter(LinkPreview.url.in_(urls)).all()} if urls else {}

    out = []
    for m in msgs:
        sender = umap.get(m.sender_id)
        rp = None
        if m.reply_to_id and m.reply_to_id in reply_msgs:
            rt = reply_msgs[m.reply_to_id]
            rts = umap.get(rt.sender_id)
            if rt.is_deleted:
                snip = 'Deleted message'
            elif rt.content:
                snip = rt.content[:120]
            elif rt.file_type:
                snip = f'[{rt.file_type}]'
            else:
                snip = ''
            rp = {'msg_id': rt.id, 'sender': rts.username if rts else '?', 'snippet': snip}
        reactions = [{'emoji': e, 'user_ids': uids} for e, uids in react_map.get(m.id, {}).items()]
        preview = serialize_preview(lp_map.get(url_map.get(m.id))) if (not m.is_deleted and m.id in url_map) else None
        out.append({
            'msg_id': m.id,
            'sender': sender.username if sender else '?',
            'sender_id': m.sender_id,
            'content': '' if m.is_deleted else m.content,
            'file_url': None if m.is_deleted else m.file_url,
            'file_type': None if m.is_deleted else m.file_type,
            'file_name': None if m.is_deleted else m.file_name,
            'timestamp': m.timestamp.strftime('%I:%M %p'),
            'ts_iso': m.timestamp.isoformat(),
            'edited': bool(m.edited),
            'is_deleted': bool(m.is_deleted),
            'reply_to': rp,
            'reactions': reactions,
            'pinned': bool(m.pinned),
            'forwarded': bool(m.forwarded),
            'preview': preview,
        })
    return out

def get_seen_state(chat_type, chat_id, me_id):
    """Return {reader_id: {'name': str, 'last_read_id': int}} for everyone
    (other than the current user) who has read messages in this chat."""
    msg_ids = [row.id for row in base_chat_query(chat_type, chat_id, me_id).with_entities(Message.id).all()]
    if not msg_ids:
        return {}
    rows = (db.session.query(MessageRead.user_id, func.max(MessageRead.message_id))
            .filter(MessageRead.message_id.in_(msg_ids))
            .filter(MessageRead.user_id != me_id)
            .group_by(MessageRead.user_id)
            .all())
    state = {}
    for reader_id, last_read in rows:
        reader = User.query.get(reader_id)
        if reader:
            state[reader_id] = {'name': reader.username, 'last_read_id': last_read}
    return state

def get_unread(chat_type, chat_id, me_id):
    """Count messages from others in this chat that the user hasn't read."""
    ids = [r.id for r in base_chat_query(chat_type, chat_id, me_id)
           .filter(Message.sender_id != me_id, Message.is_deleted == False)
           .with_entities(Message.id).all()]
    if not ids:
        return 0
    read = MessageRead.query.filter(
        MessageRead.user_id == me_id, MessageRead.message_id.in_(ids)
    ).count()
    return len(ids) - read

def extract_mentions(content, group_id):
    """Return list of user ids mentioned via @username among the group's members.
    @everyone / @all mentions every member of the group."""
    if not content or not group_id:
        return []
    group = ChatGroup.query.get(group_id)
    if not group:
        return []
    if re.search(r'@everyone\b', content, re.IGNORECASE) or re.search(r'@all\b', content, re.IGNORECASE):
        return [u.id for u in group.members]
    handles = set(h.lower() for h in re.findall(r'@([A-Za-z0-9_]+)', content))
    if not handles:
        return []
    return [u.id for u in group.members if u.username.lower() in handles]

def _participant_lists(group):
    member_ids = {u.id for u in group.members}
    members = [{'id': u.id, 'username': u.username} for u in group.members]
    non_members = [{'id': u.id, 'username': u.username}
                   for u in User.query.order_by(func.lower(User.username)).all()
                   if u.id not in member_ids]
    return members, non_members

def group_roles(group):
    admin_ids = [u.id for u in group.admins]
    if group.owner_id and group.owner_id not in admin_ids:
        admin_ids.append(group.owner_id)
    return {'owner_id': group.owner_id, 'admin_ids': admin_ids}

def is_group_admin(group, user):
    if not group:
        return False
    if group.owner_id == user.id:
        return True
    return group.admins.filter_by(id=user.id).count() > 0

def is_muted(user_id, chat_type, chat_id):
    return MutedChat.query.filter_by(user_id=user_id, chat_type=chat_type, chat_id=chat_id).first() is not None

def is_blocked_between(a, b):
    return BlockedUser.query.filter(
        or_(and_(BlockedUser.blocker_id == a, BlockedUser.blocked_id == b),
            and_(BlockedUser.blocker_id == b, BlockedUser.blocked_id == a))).first() is not None

def broadcast_participants(group):
    members, non_members = _participant_lists(group)
    payload = {
        'group_id': group.id,
        'members': members,
        'non_members': non_members,
        'member_count': len(members),
    }
    payload.update(group_roles(group))
    socketio.emit('participants_updated', payload, to=f"group_{group.id}")

# ---------- WEB PUSH ----------

def send_push_to_user(user_id, title, body, url='/dashboard'):
    """Send a Web Push notification to all of a user's subscriptions."""
    if not _vapid:
        return
    payload = json.dumps({'title': title, 'body': (body or '')[:180], 'url': url})
    for sub in PushSubscription.query.filter_by(user_id=user_id).all():
        try:
            webpush(
                subscription_info=json.loads(sub.data),
                data=payload,
                vapid_private_key=_vapid,
                vapid_claims={'sub': VAPID_CLAIM},
                ttl=120,
            )
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                db.session.delete(sub)
                db.session.commit()
        except Exception:
            pass

# ---------- LINK PREVIEWS ----------

URL_RE = re.compile(r'(https?://[^\s<>"\']+)')

def extract_first_url(text_value):
    if not text_value:
        return None
    m = URL_RE.search(text_value)
    return m.group(1).rstrip('.,);]') if m else None

def is_safe_url(url):
    """Block non-http(s) and requests to private/loopback addresses (SSRF guard)."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        infos = _socket.getaddrinfo(p.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False

def _meta_content(html, names):
    for name in names:
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']' + re.escape(name) + r'["\'][^>]*content=["\']([^"\']+)["\']',
            html, re.IGNORECASE)
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(name) + r'["\']',
                html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def serialize_preview(lp):
    if not lp or not lp.ok:
        return None
    return {'url': lp.url, 'title': lp.title, 'description': lp.description, 'image': lp.image}

def get_content_preview(content):
    url = extract_first_url(content)
    if not url:
        return None
    return serialize_preview(LinkPreview.query.filter_by(url=url).first())

def fetch_link_preview(url, room, msg_id):
    """Background task: fetch OG metadata for a URL, cache it, push to the room."""
    if not _REQUESTS_AVAILABLE or not is_safe_url(url):
        return
    with app.app_context():
        lp = LinkPreview.query.filter_by(url=url).first()
        if lp:  # already cached
            if lp.ok:
                socketio.emit('link_preview', {'msg_id': msg_id, 'preview': serialize_preview(lp)}, to=room)
            return
        title = desc = image = None
        ok = False
        try:
            resp = _requests.get(url, timeout=5, stream=True,
                                 headers={'User-Agent': 'Mozilla/5.0 (WorkspaceChat LinkBot)'})
            ctype = resp.headers.get('Content-Type', '')
            if 'text/html' in ctype:
                html = resp.raw.read(200000, decode_content=True).decode('utf-8', 'ignore')
                title = _meta_content(html, ['og:title', 'twitter:title'])
                if not title:
                    tm = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                    title = tm.group(1).strip() if tm else None
                desc = _meta_content(html, ['og:description', 'twitter:description', 'description'])
                image = _meta_content(html, ['og:image', 'twitter:image'])
                ok = bool(title or image)
            resp.close()
        except Exception:
            ok = False
        lp = LinkPreview(url=url, title=(title or '')[:400], description=(desc or '')[:700],
                         image=(image or '')[:700], ok=ok)
        try:
            db.session.add(lp)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return
        if ok:
            socketio.emit('link_preview', {'msg_id': msg_id, 'preview': serialize_preview(lp)}, to=room)

def maybe_fetch_preview(content, room, msg_id):
    url = extract_first_url(content)
    if url:
        socketio.start_background_task(fetch_link_preview, url, room, msg_id)

def push_to_offline_recipients(recipient_ids, title, body, mute_type=None, mute_id=None):
    """Push to recipients who have no active socket connection and haven't muted the chat."""
    for uid in recipient_ids:
        if uid != current_user.id and uid not in online_users:
            if mute_type and is_muted(uid, mute_type, mute_id):
                continue
            send_push_to_user(uid, title, body)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/', methods=['GET', 'POST'])
@rate_limit("8 per minute", methods=["POST"])
def login():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password')

        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))

        flash('Invalid username or password.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
@rate_limit("5 per minute", methods=["POST"])
def register():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password')

        if len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"\d", password):
            flash('Password must be 8+ chars with 1 uppercase and 1 number.')
            return redirect(url_for('register'))

        if User.query.filter(func.lower(User.username) == func.lower(username)).first():
            flash('Username already taken.')
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()

        socketio.emit('new_user_joined', {
            'id': new_user.id, 'username': new_user.username, 'profile_pic': new_user.profile_pic
        })

        login_user(new_user)
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/reset_password', methods=['GET', 'POST'])
@rate_limit("5 per minute", methods=["POST"])
def reset_password():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        new_password = request.form.get('password')

        if len(new_password) < 8 or not re.search(r"[A-Z]", new_password) or not re.search(r"\d", new_password):
            flash('Password must be 8+ chars with 1 uppercase and 1 number.')
            return redirect(url_for('reset_password'))

        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()

        if user:
            user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
            db.session.commit()
            flash('Password successfully reset! You can now log in.')
            return redirect(url_for('login'))
        else:
            flash('Username not found.')
            return redirect(url_for('reset_password'))

    return render_template('reset_password.html')

@app.route('/dashboard')
@login_required
def dashboard():
    blocked_ids = {b.blocked_id for b in BlockedUser.query.filter_by(blocker_id=current_user.id).all()}
    all_users = [u for u in User.query.filter(User.id != current_user.id).all() if u.id not in blocked_ids]
    user_groups = current_user.groups
    user_dict = {u.id: u for u in User.query.all()}
    dm_unread = {u.id: get_unread('dm', u.id, current_user.id) for u in all_users}
    group_unread = {g.id: get_unread('group', g.id, current_user.id) for g in user_groups}

    muted = [f"{m.chat_type}:{m.chat_id}" for m in MutedChat.query.filter_by(user_id=current_user.id).all()]
    archived = [f"{a.chat_type}:{a.chat_id}" for a in ArchivedChat.query.filter_by(user_id=current_user.id).all()]
    online_now = list(online_users.keys())
    last_seen = {u.id: (u.last_seen.isoformat() if u.last_seen else None) for u in all_users}

    return render_template('dashboard.html', users=all_users, groups=user_groups,
                           user_dict=user_dict, dm_unread=dm_unread, group_unread=group_unread,
                           vapid_public_key=VAPID_PUBLIC_KEY, muted=muted, archived=archived,
                           online_now=online_now, last_seen=last_seen, blocked_ids=list(blocked_ids))

@app.route('/update_username', methods=['POST'])
@login_required
@rate_limit("10 per minute", methods=["POST"])
def update_username():
    new_username = (request.form.get('username') or '').strip()
    if not new_username or len(new_username) > 150:
        return {'error': 'Invalid username.'}, 400
    existing = User.query.filter(func.lower(User.username) == func.lower(new_username)).first()
    if existing and existing.id != current_user.id:
        return {'error': 'Username already taken.'}, 400
    current_user.username = new_username
    db.session.commit()
    socketio.emit('user_renamed', {'id': current_user.id, 'username': new_username})
    return {'success': True, 'username': new_username}

@app.route('/update_password', methods=['POST'])
@login_required
@rate_limit("6 per minute", methods=["POST"])
def update_password():
    current_pw = request.form.get('current_password') or ''
    new_pw = request.form.get('new_password') or ''
    if not check_password_hash(current_user.password, current_pw):
        return {'error': 'Current password is incorrect.'}, 400
    if len(new_pw) < 8 or not re.search(r"[A-Z]", new_pw) or not re.search(r"\d", new_pw):
        return {'error': 'Password must be 8+ chars with 1 uppercase and 1 number.'}, 400
    current_user.password = generate_password_hash(new_pw, method='pbkdf2:sha256')
    db.session.commit()
    return {'success': True}

@app.route('/upload_group_photo', methods=['POST'])
@login_required
def upload_group_photo():
    group_id = int(request.form.get('group_id'))
    group = ChatGroup.query.get(group_id)
    if not group or not is_group_admin(group, current_user):
        return {'error': 'Not allowed.'}, 403
    if 'file' not in request.files:
        return {'error': 'No file part'}, 400
    file = request.files['file']
    if file and file.filename != '':
        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                file, folder="workspace_chat/groups",
                public_id=f"group_{group_id}", overwrite=True, resource_type="image")
            group.photo = result['secure_url']
        else:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'png'
            new_filename = f"group_{group_id}.{ext}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
            group.photo = url_for('static', filename=f'uploads/{new_filename}')
        db.session.commit()
        socketio.emit('group_updated', {'id': group.id, 'name': group.name, 'photo': group.photo}, to=f"group_{group.id}")
    return redirect(url_for('dashboard'))

@app.route('/save_push_subscription', methods=['POST'])
@login_required
def save_push_subscription():
    sub = request.get_json(silent=True)
    if not sub or 'endpoint' not in sub:
        return {'error': 'Invalid subscription.'}, 400
    existing = PushSubscription.query.filter_by(endpoint=sub['endpoint']).first()
    if existing:
        existing.user_id = current_user.id
        existing.data = json.dumps(sub)
    else:
        db.session.add(PushSubscription(user_id=current_user.id, endpoint=sub['endpoint'], data=json.dumps(sub)))
    db.session.commit()
    return {'success': True}

@app.route('/sw.js')
def service_worker():
    resp = app.response_class(render_template('sw.js'), mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/manifest.json')
def manifest():
    return app.response_class(render_template('manifest.json'), mimetype='application/manifest+json')

@app.route('/upload_profile', methods=['POST'])
@login_required
def upload_profile():
    if 'file' not in request.files:
        return redirect(url_for('dashboard'))
    file = request.files['file']
    if file and file.filename != '':
        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                file, folder="workspace_chat/profiles",
                public_id=f"user_{current_user.id}", overwrite=True, resource_type="image"
            )
            current_user.profile_pic = result['secure_url']
        else:
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'png'
            new_filename = f"user_{current_user.id}.{ext}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
            current_user.profile_pic = new_filename
        db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/upload_chat_file', methods=['POST'])
@login_required
def upload_chat_file():
    if 'file' not in request.files:
        return {'error': 'No file part'}, 400

    file = request.files['file']
    chat_type = request.form.get('chat_type')
    chat_id = int(request.form.get('chat_id'))
    reply_to_id = request.form.get('reply_to_id')
    reply_to_id = int(reply_to_id) if reply_to_id else None

    if chat_type == 'dm' and is_blocked_between(current_user.id, chat_id):
        return {'error': 'Blocked.'}, 403

    if file and file.filename != '':
        filename = secure_filename(file.filename)

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type:
            if mime_type.startswith('image'): file_category = 'image'
            elif mime_type.startswith('video'): file_category = 'video'
            elif mime_type.startswith('audio'): file_category = 'audio'
            else: file_category = 'document'
        else:
            file_category = 'document'

        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                file, folder="workspace_chat/files", resource_type="auto"
            )
            file_url = result['secure_url']
        else:
            unique_name = f"{uuid.uuid4().hex}_{filename}"
            file.save(os.path.join(app.config['CHAT_UPLOAD_FOLDER'], unique_name))
            file_url = url_for('static', filename=f'chat_uploads/{unique_name}')

        if chat_type == 'dm':
            new_msg = Message(sender_id=current_user.id, receiver_id=chat_id, content="",
                              file_url=file_url, file_type=file_category, file_name=filename,
                              reply_to_id=reply_to_id)
            room = dm_room(current_user.id, chat_id)
        else:
            new_msg = Message(sender_id=current_user.id, group_id=chat_id, content="",
                              file_url=file_url, file_type=file_category, file_name=filename,
                              reply_to_id=reply_to_id)
            room = f"group_{chat_id}"

        db.session.add(new_msg)
        db.session.commit()

        msg_packet = {
            'msg_id': new_msg.id, 'username': current_user.username, 'message': "",
            'file_url': file_url, 'file_type': file_category, 'file_name': filename,
            'type': chat_type, 'group_id': chat_id if chat_type == 'group' else None,
            'sender_id': current_user.id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
            'ts_iso': new_msg.timestamp.isoformat(), 'mentions': [],
            'reply_to': reply_preview(reply_to_id), 'reactions': [], 'edited': False,
            'pinned': False, 'forwarded': False, 'preview': None
        }

        if chat_type == 'dm':
            socketio.emit('receive_message', msg_packet, to=f"user_sys_{chat_id}")
            push_to_offline_recipients([chat_id], current_user.username, f"Sent an attachment: {filename}", 'dm', current_user.id)
        else:
            group = ChatGroup.query.get(chat_id)
            msg_packet['group_name'] = group.name
            for user in group.members:
                if user.id != current_user.id:
                    socketio.emit('receive_message', msg_packet, to=f"user_sys_{user.id}")
            push_to_offline_recipients([u.id for u in group.members], f"#{group.name}",
                                       f"{current_user.username} sent an attachment", 'group', chat_id)

        socketio.emit('receive_message', msg_packet, to=room)

        return {'success': True}
    return {'error': 'Upload failed'}, 400

@app.route('/create_group', methods=['POST'])
@login_required
def create_group():
    group_name = request.form.get('group_name')
    member_ids = request.form.getlist('members')
    if group_name and member_ids:
        new_group = ChatGroup(name=group_name, owner_id=current_user.id)
        new_group.members.append(current_user)
        for m_id in member_ids:
            user = User.query.get(int(m_id))
            if user: new_group.members.append(user)
        db.session.add(new_group)
        db.session.commit()
        current_user.admin_groups.append(new_group)  # creator is an admin
        db.session.commit()
        for user in new_group.members:
            socketio.emit('new_group', {'id': new_group.id, 'name': new_group.name}, to=f"user_sys_{user.id}")
    return redirect(url_for('dashboard'))

@app.route('/leave_group/<int:group_id>')
@login_required
def leave_group(group_id):
    group = ChatGroup.query.get(group_id)
    if group and current_user in group.members:
        group.members.remove(current_user)
        db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- WEBSOCKETS ---
@socketio.on('register_user')
def on_register_user():
    if not current_user.is_authenticated:
        return
    join_room(f"user_sys_{current_user.id}")
    # presence
    online_users[current_user.id] = online_users.get(current_user.id, 0) + 1
    if online_users[current_user.id] == 1:
        socketio.emit('presence_update', {'user_id': current_user.id, 'online': True})
    emit('presence_state', {'online': list(online_users.keys())})

@socketio.on('disconnect')
def on_disconnect():
    if not current_user.is_authenticated:
        return
    uid = current_user.id
    if uid in online_users:
        online_users[uid] -= 1
        if online_users[uid] <= 0:
            online_users.pop(uid, None)
            ls = None
            try:
                u = User.query.get(uid)
                if u:
                    u.last_seen = datetime.now()
                    db.session.commit()
                    ls = u.last_seen.isoformat()
            except Exception:
                db.session.rollback()
            socketio.emit('presence_update', {'user_id': uid, 'online': False, 'last_seen': ls})

@socketio.on('get_participants')
def on_get_participants(data):
    group = ChatGroup.query.get(int(data['group_id']))
    if not group or current_user not in group.members:
        return
    members, non_members = _participant_lists(group)
    payload = {'group_id': group.id, 'members': members, 'non_members': non_members,
               'can_manage': is_group_admin(group, current_user)}
    payload.update(group_roles(group))
    emit('show_participants', payload)

@socketio.on('set_group_admin')
def on_set_group_admin(data):
    group = ChatGroup.query.get(int(data['group_id']))
    user = User.query.get(int(data['user_id']))
    make_admin = bool(data.get('admin'))
    # only the owner can change admin roles
    if not group or not user or group.owner_id != current_user.id or user.id == group.owner_id:
        return
    if user not in group.members:
        return
    is_admin = group.admins.filter_by(id=user.id).count() > 0
    if make_admin and not is_admin:
        group.admins.append(user)
    elif not make_admin and is_admin:
        group.admins.remove(user)
    db.session.commit()
    broadcast_participants(group)

@socketio.on('add_participant')
def on_add_participant(data):
    group = ChatGroup.query.get(int(data['group_id']))
    user = User.query.get(int(data['user_id']))
    if not group or not user or not is_group_admin(group, current_user):
        return
    if user in group.members:
        return
    group.members.append(user)
    db.session.commit()
    socketio.emit('new_group', {'id': group.id, 'name': group.name}, to=f"user_sys_{user.id}")
    broadcast_participants(group)

@socketio.on('remove_participant')
def on_remove_participant(data):
    group = ChatGroup.query.get(int(data['group_id']))
    user = User.query.get(int(data['user_id']))
    if not group or not user or not is_group_admin(group, current_user):
        return
    if user not in group.members or user.id == group.owner_id:
        return  # can't remove the owner
    group.members.remove(user)
    if user in group.admins:
        group.admins.remove(user)
    db.session.commit()
    socketio.emit('removed_from_group', {
        'id': group.id, 'name': group.name, 'by': current_user.username
    }, to=f"user_sys_{user.id}")
    broadcast_participants(group)

@socketio.on('join_chat')
def on_join(data):
    chat_type = data['type']
    chat_id = int(data['id'])

    room = dm_room(current_user.id, chat_id) if chat_type == 'dm' else f"group_{chat_id}"
    join_room(room)

    q = base_chat_query(chat_type, chat_id, current_user.id)
    total = q.count()
    recent = q.offset(max(0, total - PAGE_SIZE)).limit(PAGE_SIZE).all()
    history = serialize_messages(recent)

    pinned = q.filter(Message.pinned == True, Message.is_deleted == False).all()
    payload = {
        'messages': history,
        'has_more': total > len(history),
        'seen': get_seen_state(chat_type, chat_id, current_user.id),
        'pinned': [_pin_preview(m) for m in pinned],
    }
    if chat_type == 'group':
        group = ChatGroup.query.get(chat_id)
        payload['members'] = [{'id': u.id, 'username': u.username} for u in group.members] if group else []
        payload['member_count'] = len(payload['members'])
        if group:
            payload.update(group_roles(group))
            payload['can_manage'] = is_group_admin(group, current_user)
    else:
        other = User.query.get(chat_id)
        payload['last_seen'] = other.last_seen.isoformat() if (other and other.last_seen) else None

    emit('load_history', payload)

@socketio.on('load_older')
def on_load_older(data):
    chat_type = data['type']
    chat_id = int(data['id'])
    before_id = int(data['before_id'])

    q = base_chat_query(chat_type, chat_id, current_user.id).filter(Message.id < before_id)
    total_older = q.count()
    older = q.offset(max(0, total_older - PAGE_SIZE)).limit(PAGE_SIZE).all()

    emit('older_history', {
        'messages': serialize_messages(older),
        'has_more': total_older > len(older),
    })

@socketio.on('mark_read')
def on_mark_read(data):
    chat_type = data['type']
    chat_id = int(data['id'])

    msgs = base_chat_query(chat_type, chat_id, current_user.id).filter(
        Message.sender_id != current_user.id
    ).all()
    if not msgs:
        return

    already = {r.message_id for r in MessageRead.query.filter(
        MessageRead.user_id == current_user.id,
        MessageRead.message_id.in_([m.id for m in msgs])
    ).all()}

    last_read_id = 0
    new_rows = False
    for m in msgs:
        last_read_id = max(last_read_id, m.id)
        if m.id not in already:
            db.session.add(MessageRead(message_id=m.id, user_id=current_user.id))
            new_rows = True
    if new_rows:
        db.session.commit()

    if last_read_id == 0:
        return

    room = dm_room(current_user.id, chat_id) if chat_type == 'dm' else f"group_{chat_id}"
    emit('messages_seen', {
        'type': chat_type, 'chat_id': chat_id,
        'reader_id': current_user.id, 'reader_name': current_user.username,
        'last_read_id': last_read_id,
    }, to=room, include_self=False)

@socketio.on('typing')
def handle_typing(data):
    chat_type, chat_id = data['type'], int(data['id'])
    room = dm_room(current_user.id, chat_id) if chat_type == 'dm' else f"group_{chat_id}"
    emit('user_typing', {'username': current_user.username}, to=room, include_self=False)

@socketio.on('send_message')
def handle_message(data):
    chat_type, chat_id, content = data['type'], int(data['id']), data['message']
    reply_to_id = data.get('reply_to_id')
    reply_to_id = int(reply_to_id) if reply_to_id else None

    if chat_type == 'dm' and is_blocked_between(current_user.id, chat_id):
        return

    if chat_type == 'dm':
        new_msg = Message(sender_id=current_user.id, receiver_id=chat_id, content=content, reply_to_id=reply_to_id)
        room = dm_room(current_user.id, chat_id)
    else:
        new_msg = Message(sender_id=current_user.id, group_id=chat_id, content=content, reply_to_id=reply_to_id)
        room = f"group_{chat_id}"

    db.session.add(new_msg)
    db.session.commit()

    mentions = extract_mentions(content, chat_id) if chat_type == 'group' else []

    msg_packet = {
        'msg_id': new_msg.id, 'username': current_user.username, 'message': content,
        'file_url': None, 'file_type': None, 'file_name': None,
        'type': chat_type, 'group_id': chat_id if chat_type == 'group' else None,
        'sender_id': current_user.id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
        'ts_iso': new_msg.timestamp.isoformat(), 'mentions': mentions,
        'reply_to': reply_preview(reply_to_id), 'reactions': [], 'edited': False,
        'pinned': False, 'forwarded': False, 'preview': None
    }

    if chat_type == 'dm':
        emit('receive_message', msg_packet, to=f"user_sys_{chat_id}")
        push_to_offline_recipients([chat_id], current_user.username, content, 'dm', current_user.id)
    else:
        group = ChatGroup.query.get(chat_id)
        msg_packet['group_name'] = group.name
        for user in group.members:
            if user.id != current_user.id:
                emit('receive_message', msg_packet, to=f"user_sys_{user.id}")
        push_to_offline_recipients([u.id for u in group.members], f"#{group.name}",
                                   f"{current_user.username}: {content}", 'group', chat_id)

    emit('receive_message', msg_packet, to=room)

    # Fetch a link preview in the background and push it to the room when ready.
    maybe_fetch_preview(content, room, new_msg.id)

@socketio.on('edit_message')
def on_edit_message(data):
    m = Message.query.get(int(data['message_id']))
    new_content = (data.get('content') or '').strip()
    if not m or m.sender_id != current_user.id or m.is_deleted or not new_content:
        return
    m.content = new_content
    m.edited = True
    db.session.commit()
    socketio.emit('message_edited', {
        'msg_id': m.id, 'content': new_content, 'edited': True
    }, to=room_for_message(m))

@socketio.on('delete_message')
def on_delete_message(data):
    m = Message.query.get(int(data['message_id']))
    if not m or m.sender_id != current_user.id or m.is_deleted:
        return
    m.is_deleted = True
    m.content = ""
    m.file_url = None
    m.file_type = None
    m.file_name = None
    db.session.commit()
    socketio.emit('message_deleted', {'msg_id': m.id}, to=room_for_message(m))

@socketio.on('toggle_reaction')
def on_toggle_reaction(data):
    m = Message.query.get(int(data['message_id']))
    emoji = (data.get('emoji') or '')[:16]
    if not m or m.is_deleted or not emoji:
        return
    existing = MessageReaction.query.filter_by(
        message_id=m.id, user_id=current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(MessageReaction(message_id=m.id, user_id=current_user.id, emoji=emoji))
    db.session.commit()
    socketio.emit('reaction_updated', {
        'msg_id': m.id, 'reactions': get_reactions(m.id)
    }, to=room_for_message(m))

@socketio.on('search_messages')
def on_search(data):
    query = (data.get('query') or '').strip()
    if len(query) < 2:
        emit('search_results', {'query': query, 'results': []})
        return

    my_group_ids = [g.id for g in current_user.groups]
    like = f"%{query}%"

    scope_conds = []
    if my_group_ids:
        scope_conds.append(Message.group_id.in_(my_group_ids))
    scope_conds.append(and_(
        Message.group_id.is_(None),
        or_(Message.sender_id == current_user.id, Message.receiver_id == current_user.id)
    ))

    msgs = Message.query.filter(
        Message.is_deleted == False,
        Message.content.isnot(None),
        Message.content.ilike(like),
        or_(*scope_conds)
    ).order_by(Message.id.desc()).limit(40).all()

    results = []
    for m in msgs:
        sender = User.query.get(m.sender_id)
        if m.group_id:
            group = ChatGroup.query.get(m.group_id)
            ctype, cid, cname = 'group', m.group_id, (group.name if group else '?')
        else:
            other_id = m.receiver_id if m.sender_id == current_user.id else m.sender_id
            other = User.query.get(other_id)
            ctype, cid, cname = 'dm', other_id, (other.username if other else '?')
        results.append({
            'msg_id': m.id, 'type': ctype, 'chat_id': cid, 'chat_name': cname,
            'sender': sender.username if sender else '?', 'content': m.content,
            'when': m.timestamp.strftime('%b %d, %I:%M %p'),
        })
    emit('search_results', {'query': query, 'results': results})

# ---------- group rename, pin, forward ----------

def _can_access_message(m):
    if not m:
        return False
    if m.group_id:
        group = ChatGroup.query.get(m.group_id)
        return bool(group and current_user in group.members)
    return current_user.id in (m.sender_id, m.receiver_id)

def _pin_preview(m):
    sender = User.query.get(m.sender_id)
    snippet = m.content or (f'[{m.file_type}]' if m.file_type else '')
    return {'msg_id': m.id, 'sender': sender.username if sender else '?', 'snippet': (snippet or '')[:120]}

@socketio.on('rename_group')
def on_rename_group(data):
    group = ChatGroup.query.get(int(data['group_id']))
    new_name = (data.get('name') or '').strip()
    if not group or not is_group_admin(group, current_user) or not new_name:
        return
    group.name = new_name[:150]
    db.session.commit()
    for u in group.members:
        socketio.emit('group_updated', {'id': group.id, 'name': group.name, 'photo': group.photo}, to=f"user_sys_{u.id}")

@socketio.on('pin_message')
def on_pin_message(data):
    m = Message.query.get(int(data['message_id']))
    if not m or m.is_deleted or not _can_access_message(m):
        return
    m.pinned = True
    db.session.commit()
    socketio.emit('message_pinned', {'msg_id': m.id, 'pinned': True, 'pin': _pin_preview(m)}, to=room_for_message(m))

@socketio.on('unpin_message')
def on_unpin_message(data):
    m = Message.query.get(int(data['message_id']))
    if not m or not _can_access_message(m):
        return
    m.pinned = False
    db.session.commit()
    socketio.emit('message_pinned', {'msg_id': m.id, 'pinned': False}, to=room_for_message(m))

@socketio.on('forward_message')
def on_forward_message(data):
    m = Message.query.get(int(data['message_id']))
    if not m or m.is_deleted or not _can_access_message(m):
        return
    target_type = data.get('target_type')
    target_id = int(data['target_id'])

    if target_type == 'group':
        group = ChatGroup.query.get(target_id)
        if not group or current_user not in group.members:
            return
        new_msg = Message(sender_id=current_user.id, group_id=target_id, content=m.content,
                          file_url=m.file_url, file_type=m.file_type, file_name=m.file_name, forwarded=True)
        room = f"group_{target_id}"
    else:
        new_msg = Message(sender_id=current_user.id, receiver_id=target_id, content=m.content,
                          file_url=m.file_url, file_type=m.file_type, file_name=m.file_name, forwarded=True)
        room = dm_room(current_user.id, target_id)

    db.session.add(new_msg)
    db.session.commit()

    packet = {
        'msg_id': new_msg.id, 'username': current_user.username, 'message': new_msg.content or "",
        'file_url': new_msg.file_url, 'file_type': new_msg.file_type, 'file_name': new_msg.file_name,
        'type': target_type, 'group_id': target_id if target_type == 'group' else None,
        'sender_id': current_user.id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
        'ts_iso': new_msg.timestamp.isoformat(), 'mentions': [],
        'reply_to': None, 'reactions': [], 'edited': False,
        'pinned': False, 'forwarded': True, 'preview': None
    }
    if target_type == 'dm':
        if is_blocked_between(current_user.id, target_id):
            return
        emit('receive_message', packet, to=f"user_sys_{target_id}")
        push_to_offline_recipients([target_id], current_user.username, packet['message'] or 'Forwarded a message', 'dm', current_user.id)
    else:
        group = ChatGroup.query.get(target_id)
        packet['group_name'] = group.name
        for u in group.members:
            if u.id != current_user.id:
                emit('receive_message', packet, to=f"user_sys_{u.id}")
        push_to_offline_recipients([u.id for u in group.members], f"#{group.name}",
                                   f"{current_user.username} forwarded a message", 'group', target_id)
    emit('receive_message', packet, to=room)


# ---------- mute / archive / block / report ----------

@socketio.on('toggle_mute')
def on_toggle_mute(data):
    ct, cid = data['chat_type'], int(data['chat_id'])
    row = MutedChat.query.filter_by(user_id=current_user.id, chat_type=ct, chat_id=cid).first()
    if row:
        db.session.delete(row); muted = False
    else:
        db.session.add(MutedChat(user_id=current_user.id, chat_type=ct, chat_id=cid)); muted = True
    db.session.commit()
    emit('mute_updated', {'chat_type': ct, 'chat_id': cid, 'muted': muted})

@socketio.on('toggle_archive')
def on_toggle_archive(data):
    ct, cid = data['chat_type'], int(data['chat_id'])
    row = ArchivedChat.query.filter_by(user_id=current_user.id, chat_type=ct, chat_id=cid).first()
    if row:
        db.session.delete(row); archived = False
    else:
        db.session.add(ArchivedChat(user_id=current_user.id, chat_type=ct, chat_id=cid)); archived = True
    db.session.commit()
    emit('archive_updated', {'chat_type': ct, 'chat_id': cid, 'archived': archived})

@socketio.on('block_user')
def on_block_user(data):
    uid = int(data['user_id'])
    if uid == current_user.id:
        return
    if not BlockedUser.query.filter_by(blocker_id=current_user.id, blocked_id=uid).first():
        db.session.add(BlockedUser(blocker_id=current_user.id, blocked_id=uid))
        db.session.commit()
    emit('block_updated', {'user_id': uid, 'blocked': True})

@socketio.on('unblock_user')
def on_unblock_user(data):
    uid = int(data['user_id'])
    row = BlockedUser.query.filter_by(blocker_id=current_user.id, blocked_id=uid).first()
    if row:
        db.session.delete(row); db.session.commit()
    emit('block_updated', {'user_id': uid, 'blocked': False})

@socketio.on('report_user')
def on_report_user(data):
    db.session.add(Report(
        reporter_id=current_user.id,
        reported_user_id=int(data['user_id']) if data.get('user_id') else None,
        message_id=int(data['message_id']) if data.get('message_id') else None,
        reason=(data.get('reason') or '')[:500]))
    db.session.commit()
    emit('report_ack', {'ok': True})

@socketio.on('schedule_message')
def on_schedule_message(data):
    ct, cid = data['type'], int(data['id'])
    content = (data.get('message') or '').strip()
    if not content:
        return
    try:
        send_at = datetime.fromisoformat(data['send_at'])
    except Exception:
        return
    if ct == 'dm' and is_blocked_between(current_user.id, cid):
        return
    db.session.add(ScheduledMessage(sender_id=current_user.id, chat_type=ct, chat_id=cid,
                                    content=content, send_at=send_at, sent=False))
    db.session.commit()
    emit('scheduled_ack', {'ok': True, 'send_at': send_at.isoformat()})

@app.route('/export_chat')
@login_required
def export_chat():
    ct = request.args.get('type')
    cid = int(request.args.get('id'))
    if ct == 'group':
        group = ChatGroup.query.get(cid)
        if not group or current_user not in group.members:
            return "Not allowed", 403
        title = f"# {group.name}"
    else:
        other = User.query.get(cid)
        title = f"DM with {other.username if other else cid}"
    msgs = base_chat_query(ct, cid, current_user.id).all()
    lines = [f"Chat export: {title}", "=" * 40, ""]
    for m in msgs:
        sender = User.query.get(m.sender_id)
        ts = m.timestamp.strftime('%Y-%m-%d %H:%M')
        if m.is_deleted:
            body = "[deleted]"
        elif m.content:
            body = m.content
        elif m.file_name:
            body = f"[file: {m.file_name}]"
        else:
            body = ""
        lines.append(f"[{ts}] {sender.username if sender else '?'}: {body}")
    text_out = "\n".join(lines)
    return app.response_class(
        text_out, mimetype='text/plain',
        headers={'Content-Disposition': 'attachment; filename="chat_export.txt"'})

# ---------- scheduled message delivery (background) ----------

def deliver_scheduled(sm):
    sender = User.query.get(sm.sender_id)
    if not sender:
        return
    if sm.chat_type == 'dm':
        if is_blocked_between(sm.sender_id, sm.chat_id):
            return
        new_msg = Message(sender_id=sm.sender_id, receiver_id=sm.chat_id, content=sm.content)
        room = dm_room(sm.sender_id, sm.chat_id)
    else:
        new_msg = Message(sender_id=sm.sender_id, group_id=sm.chat_id, content=sm.content)
        room = f"group_{sm.chat_id}"
    db.session.add(new_msg)
    db.session.commit()
    mentions = extract_mentions(sm.content, sm.chat_id) if sm.chat_type == 'group' else []
    packet = {
        'msg_id': new_msg.id, 'username': sender.username, 'message': sm.content,
        'file_url': None, 'file_type': None, 'file_name': None,
        'type': sm.chat_type, 'group_id': sm.chat_id if sm.chat_type == 'group' else None,
        'sender_id': sm.sender_id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
        'ts_iso': new_msg.timestamp.isoformat(), 'mentions': mentions,
        'reply_to': None, 'reactions': [], 'edited': False,
        'pinned': False, 'forwarded': False, 'preview': None
    }
    if sm.chat_type == 'dm':
        socketio.emit('receive_message', packet, to=f"user_sys_{sm.chat_id}")
        if sm.chat_id not in online_users and not is_muted(sm.chat_id, 'dm', sm.sender_id):
            send_push_to_user(sm.chat_id, sender.username, sm.content)
    else:
        group = ChatGroup.query.get(sm.chat_id)
        if group:
            packet['group_name'] = group.name
            for u in group.members:
                if u.id != sm.sender_id:
                    socketio.emit('receive_message', packet, to=f"user_sys_{u.id}")
                    if u.id not in online_users and not is_muted(u.id, 'group', sm.chat_id):
                        send_push_to_user(u.id, f"#{group.name}", f"{sender.username}: {sm.content}")
    socketio.emit('receive_message', packet, to=room)

def scheduled_poller():
    while True:
        socketio.sleep(20)
        try:
            with app.app_context():
                due = ScheduledMessage.query.filter(
                    ScheduledMessage.sent == False,
                    ScheduledMessage.send_at <= datetime.now()
                ).all()
                for sm in due:
                    sm.sent = True
                    db.session.commit()
                    try:
                        deliver_scheduled(sm)
                    except Exception:
                        db.session.rollback()
        except Exception:
            pass


# Run migrations at import time so it also works under gunicorn/gevent on Heroku.
safe_auto_migrate()
socketio.start_background_task(scheduled_poller)

if __name__ == '__main__':
    socketio.run(app, debug=True)
