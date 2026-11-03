# The web app for any container host (Fly, Render, Railway, a VPS). Modal has its own
# deploy/modal_app.py. Pass the model keys as environment variables (see .env.example).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt

COPY oneshot ./oneshot
COPY web ./web

ENV BUDGET_FILE=/data/budget.json
ENV PRIVATE_DATA_DIR=/data/private
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
