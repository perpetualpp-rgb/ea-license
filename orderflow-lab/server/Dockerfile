# Flow Lab API image. UNTESTED: no Docker daemon was available when this was written.
# Build from orderflow-lab/ (not server/), because the API imports the order-flow engine:
#   docker build -f server/Dockerfile -t flowlab-api .
# app/orderflow.py adds the parent of server/ to sys.path and imports engine/ and feeds/ from
# there, so the image keeps the repository layout: /srv/engine, /srv/feeds, /srv/server.
FROM python:3.13-slim
WORKDIR /srv/server
COPY server/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY engine /srv/engine
COPY feeds /srv/feeds
COPY server/app app
COPY server/migrations migrations
COPY server/migrate.py server/ingest.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# set FLOWLAB_DATABASE_URL / FLOWLAB_JWT_SECRET at run time (never bake them into the image)
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
