FROM golang:1.26-alpine AS build
WORKDIR /src

RUN apk add --no-cache git ca-certificates

COPY go.mod ./
COPY main.go ./

RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /app/batch-transcoder main.go

FROM alpine:latest
RUN adduser -D -u 1000 appuser && \
    apk add --no-cache ca-certificates tzdata && \
    mkdir -p /app /input /output && \
    chown -R appuser:appuser /app /input /output

COPY --from=build /app/batch-transcoder /usr/local/bin/batch-transcoder

USER appuser
WORKDIR /app
ENTRYPOINT ["/usr/local/bin/batch-transcoder"]