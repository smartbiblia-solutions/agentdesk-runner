FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# (Optionnal but recommanded) non-root user
RUN useradd -m appuser

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install -U pip
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code
COPY ./runner /app/runner
COPY ./config/config.json /home/appuser/.nanobot/config.json
RUN chown -R appuser:appuser /home/appuser/.nanobot /app

EXPOSE 8000

USER appuser

CMD ["sh", "-c", "uvicorn runner.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 0"]
