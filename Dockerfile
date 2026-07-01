FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.example.json .

RUN mkdir -p /data

ENV BRIDGE_DB_PATH=/data/bridge.db

CMD ["python", "main.py"]
