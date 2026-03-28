FROM python:3.12 AS base
RUN pip install --upgrade pip
WORKDIR /app
RUN --mount=source=/requirements.txt,target=/app/requirements.txt \
    --mount=type=cache,sharing=private,target=/root/.cache        \
  pip install -r requirements.txt

FROM base AS runtime
COPY --link .env .env
COPY --link src/ .
ENTRYPOINT [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000" ]
