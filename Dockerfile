# ============================================================
# Z12 AI CFO Suite - Dockerfile Multi-Stage Build
# ZAKI OS Platform v3.2 - React 18 + TypeScript + Vite + Nginx
# ============================================================

# -- Stage 1: Builder (Node 20 Alpine) ----------------------
FROM node:20-alpine AS builder

LABEL maintainer="zakibelm" \
      description="Z12 AI CFO Suite - Bureau CPA Virtuel IA" \
      version="3.2.0"

WORKDIR /app

# Install dependencies first (leverage Docker cache)
COPY package.json package-lock.json* ./
# Audit 2026-11 : pas de repli sur "npm install" (il ignorerait le lockfile) ; l'échec doit être visible.
RUN npm ci

# Copy all source files
COPY . .

# Build production bundle (TypeScript compile + Vite bundle)
RUN npm run build

# -- Stage 2: Production Nginx Server -----------------------
FROM nginx:1.25-alpine AS production

# Install curl for health checks
RUN apk add --no-cache curl

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built React app from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port 80
EXPOSE 80

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost/ || exit 1

# Start nginx in foreground
CMD ["nginx", "-g", "daemon off;"]
