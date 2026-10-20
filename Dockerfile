ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend ./frontend
COPY scripts/build.js ./scripts/build.js
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=staging BOOKING_WRITES_ENABLED=false
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
COPY app.py consent.py database.py db_transport.py ops.py resilience.py security.py telemetry.py tools.py voice.py workflow.py ./
COPY instructions.txt policies.json ./
COPY --from=frontend /build/dist ./dist
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/health/live', timeout=3)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]
