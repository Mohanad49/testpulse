# syntax=docker/dockerfile:1

FROM node:24-alpine AS builder
WORKDIR /app
RUN corepack enable

COPY packages/testpulse-web/package.json packages/testpulse-web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm-store \
    pnpm config set store-dir /pnpm-store && pnpm install --frozen-lockfile

COPY packages/testpulse-web/ ./
RUN pnpm build


FROM nginx:1.27-alpine AS runtime
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
