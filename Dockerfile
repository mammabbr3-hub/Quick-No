FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY README.md ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && pip install --no-cache-dir --no-build-isolation .
CMD ["python", "-m", "app.main"]
