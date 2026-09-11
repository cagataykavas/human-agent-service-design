FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml service_design.py ./
COPY app ./app
COPY human_agent ./human_agent
COPY service_desk ./service_desk
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE_DESK_DATABASE_PATH=/data/service-desk.db

RUN addgroup --system service && adduser --system --ingroup service service
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -r /wheels

WORKDIR /app
RUN mkdir -p /data && chown service:service /data
USER service
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
