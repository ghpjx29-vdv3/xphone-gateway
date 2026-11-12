package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/coder/websocket"
	"github.com/hashicorp/yamux"
)

type credentials struct {
	ID  string `json:"gatewayId"`
	Key string `json:"relaySecret"`
}

func runConnector(ctx context.Context, address, path string) error {
	endpoint, err := url.Parse(address)
	if err != nil || endpoint.Scheme != "wss" || endpoint.Hostname() == "" || endpoint.User != nil ||
		endpoint.Path != prefix+"/connect" || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return fmt.Errorf("invalid XPhone connection address")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var binding credentials
	if err := json.Unmarshal(data, &binding); err != nil || !gatewayID.MatchString(binding.ID) || !relayKey.MatchString(binding.Key) {
		return fmt.Errorf("invalid XPhone connection credentials")
	}

	delay := time.Second
	for ctx.Err() == nil {
		started := time.Now()
		err := connect(ctx, address, binding, runtimeAddr)
		if ctx.Err() != nil {
			return nil
		}
		log.Printf("XPhone connection interrupted: %s", err)
		if time.Since(started) > 30*time.Second {
			delay = time.Second
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
		delay = min(15*time.Second, 2*delay)
	}
	return nil
}

func connect(ctx context.Context, address string, binding credentials, localAddr string) error {
	handshake, cancel := context.WithTimeout(ctx, 12*time.Second)
	headers := make(http.Header)
	headers.Set("Authorization", "Bearer "+binding.Key)
	headers.Set("X-XPhone-Gateway-Id", binding.ID)
	headers.Set("User-Agent", "XPhone-Gateway/1.0")
	socket, response, err := websocket.Dial(handshake, address, &websocket.DialOptions{
		HTTPHeader: headers,
		HTTPClient: &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
	})
	cancel()
	if err != nil {
		if response != nil {
			return fmt.Errorf("relay handshake HTTP %d", response.StatusCode)
		}
		return fmt.Errorf("relay handshake: %w", err)
	}
	conn := websocket.NetConn(ctx, socket, websocket.MessageBinary)
	defer conn.Close()
	session, err := yamux.Server(conn, sessionConfig())
	if err != nil {
		return err
	}
	defer session.Close()
	stop := context.AfterFunc(ctx, func() { _ = session.Close() })
	defer stop()
	for {
		stream, err := session.AcceptStream()
		if err != nil {
			return err
		}
		go forwardStream(ctx, stream, localAddr)
	}
}

func forwardStream(ctx context.Context, stream *yamux.Stream, localAddr string) {
	defer stream.Close()
	dialer := net.Dialer{Timeout: 3 * time.Second}
	local, err := dialer.DialContext(ctx, "tcp", localAddr)
	if err != nil {
		return
	}
	defer local.Close()
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = io.Copy(local, stream)
		_ = local.(*net.TCPConn).CloseWrite()
	}()
	_, _ = io.Copy(stream, local)
	_ = stream.Close()
	<-done
}
