FROM python:3.12-slim

# ffmpeg for audio extract/transcode. Deno is the JavaScript runtime yt-dlp uses
# to solve YouTube's signature / n-challenge (without it, web-client formats come
# back as "Only images available" — see yt-dlp wiki/EJS).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 9000
# Self-heal: pull the newest yt-dlp AND the bgutil PO-token plugin on every start
# so extractor fixes land without rebuilding the image. Then run the API.
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp bgutil-ytdlp-pot-provider >/dev/null 2>&1 || true; uvicorn app:app --host 0.0.0.0 --port 9000"]
