FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000
# The start-up check runs first. With store accounts on, the server does
# not start unless the database answers and sign-in can work, so a wrong
# setting fails the deploy (the previous version keeps running) instead of
# putting up a site nobody can use. With accounts off it does nothing.
#
# 1 worker (jobs live in its memory) + threads so progress polling and
# downloads are answered while a reconciliation run is working.
CMD ["sh", "-c", "python -m accounts.preflight && exec gunicorn app:app --timeout 300 --workers 1 --threads 4 --bind 0.0.0.0:10000"]
