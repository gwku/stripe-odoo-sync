# syntax=docker/dockerfile:1
FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a
RUN apk add --no-cache tzdata ca-certificates \
    && addgroup -g 10001 stripesync \
    && adduser -D -H -u 10001 -G stripesync stripesync
WORKDIR /app
COPY stripe_odoo /app/stripe_odoo
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "stripe_odoo"]
CMD ["sync"]
LABEL org.opencontainers.image.title="stripe-odoo-sync"
LABEL org.opencontainers.image.description="Weekly Stripe to Odoo 19 invoice sync"
