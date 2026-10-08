FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY tests ./tests
RUN pip install '.[dev]'
USER app
ENV COVERAGE_FILE=/tmp/.coverage
CMD ["sh", "-c", "ruff check --no-cache . && mypy --cache-dir=/tmp/mypy-cache app && pytest -p no:cacheprovider --cov=app --cov-report=term-missing"]
