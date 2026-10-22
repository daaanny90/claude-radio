FROM python:3.12-slim

# libopus to decode/encode audio, tzdata so the logs use the local time zone
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY radio_bot.py board.py sounds.py sampler.py summary.py smoke_test.py ./
CMD ["python", "-u", "radio_bot.py"]
