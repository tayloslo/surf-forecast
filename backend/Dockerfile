FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite DB lives on a mounted volume at /data so it survives deploys and
# restarts. See fly.toml for the volume mount.
ENV SURF_DB_PATH=/data/surf_spots.db
ENV PORT=8080

EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
