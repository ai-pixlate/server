FROM python:3.12-alpine
ARG FLAVOR
ARG SLOW=
ARG CRASH=
LABEL flavor=$FLAVOR
WORKDIR /app
COPY app /app/app
COPY fakeproc /usr/local/bin/uvicorn
COPY fakeproc /usr/local/bin/celery
COPY celery_main.py /usr/local/lib/python3.12/site-packages/celery/__main__.py
RUN touch /usr/local/lib/python3.12/site-packages/celery/__init__.py
RUN chmod +x /usr/local/bin/uvicorn /usr/local/bin/celery && if [ -n "$SLOW" ]; then echo "$SLOW" > /slow; fi && if [ -n "$CRASH" ]; then touch /crash; fi
