FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY src/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home appuser \
    && mkdir /app/runtime && chown appuser:appuser /app/runtime
COPY src/*.py src/*.html src/*.css src/*.js ./
COPY src/frontend_vendor/ ./frontend_vendor/
COPY src/scripts/ ./scripts/
COPY data/demo/ ./data/demo/
USER appuser
EXPOSE 8000
CMD ["python", "scripts/container_start.py"]
