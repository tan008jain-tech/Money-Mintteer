FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY money_mintter ./money_mintter

RUN pip install --no-cache-dir .

CMD ["python", "-m", "money_mintter.main"]
