package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hashicorp/yamux"
)

const (
	prefix       = "/api/xphone-vps"
	runtimeAddr  = "127.0.0.1:8765"
	accountRoute = "http://127.0.0.1:8790/api/agent-worker/gateway/resolve"
)

func sessionConfig() *yamux.Config {
	config := yamux.DefaultConfig()
	config.KeepAliveInterval = 20 * time.Second
	config.StreamOpenTimeout = 10 * time.Second
	config.ConnectionWriteTimeout = 10 * time.Second
	return config
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil {
		log.Fatal(err)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 3 && args[0] == "connect" {
		return runConnector(ctx, args[1], args[2])
	}
	if len(args) != 1 || args[0] != "serve" {
		return fmt.Errorf("usage: xphone-vps-relay serve | connect WSS_URL BINDING_FILE")
	}
	key := os.Getenv("XPHONE_GATEWAY_LOOKUP_SECRET")
	if len(key) < 32 {
		return fmt.Errorf("XPhone account authentication is not configured")
	}
	relay := newRelay(accountRoute, key)
	defer relay.close()
	server := &http.Server{
		Addr: "127.0.0.1:8796", Handler: relay.routes(),
		ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 64 << 10,
	}
	context.AfterFunc(ctx, func() {
		relay.close()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	})
	if err := server.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}
