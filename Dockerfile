FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.3.2 \
    && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY src ./src
RUN poetry install --only main

EXPOSE 8000
CMD ["uvicorn", "openagent_control.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
