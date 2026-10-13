FROM python:3.12-alpine
ARG FLAVOR
ARG SLOW=
ARG CRASH=
LABEL flavor=$FLAVOR
WORKDIR /app
COPY app /app/app
COPY fakeproc /usr/local/bin/uvicorn
COPY fakeproc /usr/local/bin/celery
RUN chmod +x /usr/local/bin/uvicorn /usr/local/bin/celery && if [ -n "$SLOW" ]; then echo "$SLOW" > /slow; fi && if [ -n "$CRASH" ]; then touch /crash; fi
