FROM python:3.15 AS base

RUN --mount=type=cache,sharing=private,target=/root/.cache \
  pip install -r requirements

FROM base AS runtime
# to be seen

