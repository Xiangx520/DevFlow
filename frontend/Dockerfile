FROM python:3.12-slim-bookworm
WORKDIR /app
COPY frontend/requirements.lock ./requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.lock
COPY frontend/server.py frontend/index.html ./
COPY frontend/src ./src
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
