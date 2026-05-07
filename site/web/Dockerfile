FROM node:24-alpine AS build

WORKDIR /src
COPY site/web/package.json site/web/package-lock.json ./
RUN npm ci
COPY site/web/ .
COPY mocks/web/src/styles/tokens.css /mocks/web/src/styles/tokens.css
RUN npm run build

FROM alpine:3.22

RUN mkdir -p /srv/www
COPY --from=build /src/dist /opt/site-dist
CMD ["sh", "-c", "rm -rf /srv/www/* && cp -a /opt/site-dist/. /srv/www/"]
