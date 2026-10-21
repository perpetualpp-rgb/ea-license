# build from orderflow-lab/:  docker build -f live/Dockerfile -t orderflow-collector .
FROM python:3.12-slim
WORKDIR /app
COPY live/requirements.txt live/requirements.txt
RUN pip install --no-cache-dir -r live/requirements.txt
COPY feeds feeds
COPY engine engine
COPY live live
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "live.collector"]
