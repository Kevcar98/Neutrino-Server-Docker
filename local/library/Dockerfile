FROM python:3.12-slim

# ffmpeg: remuxes M4A/MP4 uploads to "faststart" (moov atom up front) so they
# can be progressively streamed by desktop players instead of fully downloaded.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8091
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8091"]
