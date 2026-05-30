FROM node:24-alpine AS build

WORKDIR /src
ARG PUBLIC_CREWDAY_APP_ORIGIN=https://app.crew.day
ENV PUBLIC_CREWDAY_APP_ORIGIN=$PUBLIC_CREWDAY_APP_ORIGIN
COPY site/web/package.json site/web/package-lock.json ./
RUN npm ci --min-release-age=7
COPY site/web/ .
COPY app/web/src/styles/tokens.css /app/web/src/styles/tokens.css
RUN npm run build

FROM alpine:3.22

RUN mkdir -p /srv/www
COPY --from=build /src/dist /opt/site-dist
CMD ["sh", "-c", "rm -rf /srv/www/* && cp -a /opt/site-dist/. /srv/www/"]
