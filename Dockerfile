# Workspace Chat — production image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for better layer caching. psycopg2-binary, gevent and
# cryptography all ship manylinux wheels, so no system build tools are needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Single gevent worker — matches the Heroku Procfile. The app keeps presence
# state in memory and Socket.IO has no message queue, so do NOT raise -w above 1
# without first adding Redis (SocketIO(message_queue=...)).
CMD ["gunicorn", "--worker-class", "gevent", "-w", "1", "-b", "0.0.0.0:8000", "--access-logfile", "-", "app:app"]
