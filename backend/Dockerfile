FROM docker:29-cli AS dockercli
FROM python:3.12-slim-bookworm
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY backend/requirements.lock ./requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.lock
COPY backend/pyproject.toml ./
COPY backend/devflow ./devflow
RUN pip install --no-deps .
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/tests ./tests
CMD ["uvicorn","devflow.api:app","--host","0.0.0.0","--port","8000"]
