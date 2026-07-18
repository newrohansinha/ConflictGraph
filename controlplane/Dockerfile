FROM golang:1.25-alpine AS build
WORKDIR /src
COPY controlplane/go.mod controlplane/go.sum* ./
RUN go mod download
COPY controlplane/ .
ARG VERSION=container
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/conflictgraph ./cmd/conflictgraph

FROM alpine:3.24
RUN addgroup -S conflictgraph && adduser -S -G conflictgraph conflictgraph && apk add --no-cache ca-certificates wget
COPY --from=build /out/conflictgraph /usr/local/bin/conflictgraph
WORKDIR /app
USER conflictgraph
EXPOSE 8080
ENTRYPOINT ["conflictgraph"]
