FROM node:24-alpine AS build

WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM alpine:3.22

RUN mkdir -p /srv/www
COPY --from=build /src/dist /opt/site-dist
CMD ["sh", "-c", "rm -rf /srv/www/* && cp -a /opt/site-dist/. /srv/www/"]
