from gevent import monkey
monkey.patch_all()
import os
import re
import uuid
import mimetypes
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit, join_room

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
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Upgraded to 50MB to handle MP4s
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['CHAT_UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# How many messages to load per "page" when opening a chat / loading older history.
PAGE_SIZE = 30

group_members = db.Table('group_members',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('chat_group.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    profile_pic = db.Column(db.String(150), default='default')
    groups = db.relationship('ChatGroup', secondary=group_members, backref=db.backref('members', lazy='dynamic'))

class ChatGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)

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

# Tracks which user has read which message (powers "Seen" / "Seen by N").
class MessageRead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='uq_message_user_read'),)


def safe_auto_migrate():
    """Create any missing tables without touching existing data.

    On the live Heroku Postgres DB the User/Message/ChatGroup tables already
    exist with data. create_all() only creates tables that don't yet exist, so
    it will add the new message_read table while leaving existing rows intact.
    """
    with app.app_context():
        db.create_all()


# ---------- DM/GROUP HELPERS ----------

def dm_room(a, b):
    return f"dm_{min(a, b)}_{max(a, b)}"

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

def serialize_message(m):
    return {
        'msg_id': m.id,
        'sender': User.query.get(m.sender_id).username,
        'sender_id': m.sender_id,
        'content': m.content,
        'file_url': m.file_url,
        'file_type': m.file_type,
        'file_name': m.file_name,
        'timestamp': m.timestamp.strftime('%I:%M %p'),
    }

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

def extract_mentions(content, group_id):
    """Return list of user ids mentioned via @username among the group's members."""
    if not content or not group_id:
        return []
    handles = set(h.lower() for h in re.findall(r'@([A-Za-z0-9_]+)', content))
    if not handles:
        return []
    group = ChatGroup.query.get(group_id)
    if not group:
        return []
    return [u.id for u in group.members if u.username.lower() in handles]


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/', methods=['GET', 'POST'])
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
    all_users = User.query.filter(User.id != current_user.id).all()
    user_groups = current_user.groups
    user_dict = {u.id: u for u in User.query.all()}
    return render_template('dashboard.html', users=all_users, groups=user_groups, user_dict=user_dict)

@app.route('/upload_profile', methods=['POST'])
@login_required
def upload_profile():
    if 'file' not in request.files:
        return redirect(url_for('dashboard'))
    file = request.files['file']
    if file and file.filename != '':
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

    if file and file.filename != '':
        filename = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{filename}"
        file_path = os.path.join(app.config['CHAT_UPLOAD_FOLDER'], unique_name)
        file.save(file_path)

        file_url = url_for('static', filename=f'chat_uploads/{unique_name}')

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type:
            if mime_type.startswith('image'): file_category = 'image'
            elif mime_type.startswith('video'): file_category = 'video'
            elif mime_type.startswith('audio'): file_category = 'audio'
            else: file_category = 'document'
        else:
            file_category = 'document'

        if chat_type == 'dm':
            new_msg = Message(sender_id=current_user.id, receiver_id=chat_id, content="", file_url=file_url, file_type=file_category, file_name=filename)
            room = dm_room(current_user.id, chat_id)
        else:
            new_msg = Message(sender_id=current_user.id, group_id=chat_id, content="", file_url=file_url, file_type=file_category, file_name=filename)
            room = f"group_{chat_id}"

        db.session.add(new_msg)
        db.session.commit()

        msg_packet = {
            'msg_id': new_msg.id, 'username': current_user.username, 'message': "",
            'file_url': file_url, 'file_type': file_category, 'file_name': filename,
            'type': chat_type, 'group_id': chat_id if chat_type == 'group' else None,
            'sender_id': current_user.id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
            'mentions': []
        }

        if chat_type == 'dm':
            socketio.emit('receive_message', msg_packet, to=f"user_sys_{chat_id}")
        else:
            group = ChatGroup.query.get(chat_id)
            msg_packet['group_name'] = group.name
            for user in group.members:
                if user.id != current_user.id:
                    socketio.emit('receive_message', msg_packet, to=f"user_sys_{user.id}")

        socketio.emit('receive_message', msg_packet, to=room)

        return {'success': True}
    return {'error': 'Upload failed'}, 400

@app.route('/create_group', methods=['POST'])
@login_required
def create_group():
    group_name = request.form.get('group_name')
    member_ids = request.form.getlist('members')
    if group_name and member_ids:
        new_group = ChatGroup(name=group_name)
        new_group.members.append(current_user)
        for m_id in member_ids:
            user = User.query.get(int(m_id))
            if user: new_group.members.append(user)
        db.session.add(new_group)
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
    join_room(f"user_sys_{current_user.id}")

def _participant_lists(group):
    """Return (members, non_members) as lists of {id, username}."""
    member_ids = {u.id for u in group.members}
    members = [{'id': u.id, 'username': u.username} for u in group.members]
    non_members = [{'id': u.id, 'username': u.username}
                   for u in User.query.order_by(func.lower(User.username)).all()
                   if u.id not in member_ids]
    return members, non_members

def broadcast_participants(group):
    """Tell everyone currently viewing the group that its membership changed."""
    members, non_members = _participant_lists(group)
    socketio.emit('participants_updated', {
        'group_id': group.id,
        'members': members,
        'non_members': non_members,
        'member_count': len(members),
    }, to=f"group_{group.id}")

@socketio.on('get_participants')
def on_get_participants(data):
    group = ChatGroup.query.get(int(data['group_id']))
    if not group or current_user not in group.members:
        return
    members, non_members = _participant_lists(group)
    emit('show_participants', {
        'group_id': group.id,
        'members': members,
        'non_members': non_members,
    })

@socketio.on('add_participant')
def on_add_participant(data):
    group = ChatGroup.query.get(int(data['group_id']))
    user = User.query.get(int(data['user_id']))
    # Only existing members can manage membership.
    if not group or not user or current_user not in group.members:
        return
    if user in group.members:
        return
    group.members.append(user)
    db.session.commit()
    # Make the group appear in the newly added user's sidebar.
    socketio.emit('new_group', {'id': group.id, 'name': group.name}, to=f"user_sys_{user.id}")
    broadcast_participants(group)

@socketio.on('remove_participant')
def on_remove_participant(data):
    group = ChatGroup.query.get(int(data['group_id']))
    user = User.query.get(int(data['user_id']))
    if not group or not user or current_user not in group.members:
        return
    if user not in group.members:
        return
    group.members.remove(user)
    db.session.commit()
    # Tell the removed user so the group disappears from their app.
    socketio.emit('removed_from_group', {
        'id': group.id, 'name': group.name, 'by': current_user.username
    }, to=f"user_sys_{user.id}")
    broadcast_participants(group)

@socketio.on('join_chat')
def on_join(data):
    chat_type = data['type']
    chat_id = int(data['id'])

    if chat_type == 'dm':
        room = dm_room(current_user.id, chat_id)
    else:
        room = f"group_{chat_id}"

    join_room(room)

    q = base_chat_query(chat_type, chat_id, current_user.id)
    total = q.count()
    # Load only the most recent PAGE_SIZE messages; older ones load on demand.
    recent = q.offset(max(0, total - PAGE_SIZE)).limit(PAGE_SIZE).all()
    history = [serialize_message(m) for m in recent]

    payload = {
        'messages': history,
        'has_more': total > len(history),
        'seen': get_seen_state(chat_type, chat_id, current_user.id),
    }
    if chat_type == 'group':
        group = ChatGroup.query.get(chat_id)
        payload['members'] = [{'id': u.id, 'username': u.username} for u in group.members] if group else []
        payload['member_count'] = len(payload['members'])

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
        'messages': [serialize_message(m) for m in older],
        'has_more': total_older > len(older),
    })

@socketio.on('mark_read')
def on_mark_read(data):
    """Mark every message in this chat (not sent by me) as read by me, then
    tell the room so senders can update their 'Seen' indicators."""
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
        'type': chat_type,
        'chat_id': chat_id,
        'reader_id': current_user.id,
        'reader_name': current_user.username,
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

    if chat_type == 'dm':
        new_msg = Message(sender_id=current_user.id, receiver_id=chat_id, content=content)
        room = dm_room(current_user.id, chat_id)
    else:
        new_msg = Message(sender_id=current_user.id, group_id=chat_id, content=content)
        room = f"group_{chat_id}"

    db.session.add(new_msg)
    db.session.commit()

    mentions = extract_mentions(content, chat_id) if chat_type == 'group' else []

    msg_packet = {
        'msg_id': new_msg.id, 'username': current_user.username, 'message': content,
        'file_url': None, 'file_type': None, 'file_name': None,
        'type': chat_type, 'group_id': chat_id if chat_type == 'group' else None,
        'sender_id': current_user.id, 'timestamp': new_msg.timestamp.strftime('%I:%M %p'),
        'mentions': mentions
    }

    if chat_type == 'dm':
        emit('receive_message', msg_packet, to=f"user_sys_{chat_id}")
    else:
        group = ChatGroup.query.get(chat_id)
        msg_packet['group_name'] = group.name
        for user in group.members:
            if user.id != current_user.id:
                emit('receive_message', msg_packet, to=f"user_sys_{user.id}")

    emit('receive_message', msg_packet, to=room)


# Run migrations at import time so it also works under gunicorn/gevent on Heroku.
safe_auto_migrate()

if __name__ == '__main__':
    socketio.run(app, debug=True)
