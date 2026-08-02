FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        hdparm smartmontools \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask

WORKDIR /app
COPY app/ ./

EXPOSE 8763
CMD ["python", "app.py"]
