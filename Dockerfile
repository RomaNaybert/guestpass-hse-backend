FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home guestpass
COPY --chown=guestpass:guestpass app ./app
COPY --chown=guestpass:guestpass tests ./tests
COPY --chown=guestpass:guestpass pyproject.toml ./pyproject.toml
USER guestpass
EXPOSE 8080
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
