FROM scratch AS wheelhouse
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt .
ARG PIP_NO_INDEX=0
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN --mount=from=wheelhouse,target=/wheels pip install --find-links=/wheels --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY backend/app ./app
COPY backend/tests ./tests
COPY backend/pytest.ini ./pytest.ini
COPY notebooks /opt/baselines
RUN useradd -m -u 1000 researcher && mkdir -p /data && chown researcher /data
ENV PYTHONPATH=/app
USER researcher
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
