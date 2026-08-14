FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY commander/ ./commander/
COPY worker/ ./worker/
COPY shared/ ./shared/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "commander.bot"]
