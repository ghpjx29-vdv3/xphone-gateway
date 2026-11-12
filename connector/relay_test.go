package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestFiftyGatewaySessions(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	bindings := make([]credentials, 50)
	keys := make(map[string]string, len(bindings))
	for index := range bindings {
		id := fmt.Sprintf("00000000-0000-0000-0000-%012d", index+1)
		digest := sha256.Sum256([]byte(id))
		key := base64.RawURLEncoding.EncodeToString(digest[:])
		bindings[index] = credentials{id, key}
		keys[id] = key
	}
	lookupKey := strings.Repeat("l", 43)
	account := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var query map[string]string
		_ = json.NewDecoder(r.Body).Decode(&query)
		id := query["gatewayId"]
		if r.Header.Get("Authorization") != "Bearer "+lookupKey || keys[id] == "" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"gatewayId": id, "relaySecret": keys[id]})
	}))
	defer account.Close()
	relay := newRelay(account.URL, lookupKey)
	defer relay.close()
	public := httptest.NewServer(relay.routes())
	defer public.Close()
	publicWS := "ws" + strings.TrimPrefix(public.URL, "http")
	runtime := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pair/start":
			w.Header().Set("X-Test-Query", r.URL.RawQuery)
			_, _ = io.Copy(w, r.Body)
		case "/client/ws":
			c, err := websocket.Accept(w, r, nil)
			if err != nil {
				return
			}
			defer c.CloseNow()
			mode, message, err := c.Read(r.Context())
			if err == nil {
				_ = c.Write(r.Context(), mode, append([]byte(r.URL.Query().Get("account")+":"), message...))
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer runtime.Close()
	agentURL := publicWS + prefix + "/connect"

	var agents sync.WaitGroup
	start := func() context.CancelFunc {
		agentCtx, stop := context.WithCancel(ctx)
		for _, binding := range bindings {
			agents.Add(1)
			go func() {
				defer agents.Done()
				_ = connect(agentCtx, agentURL, binding, strings.TrimPrefix(runtime.URL, "http://"))
			}()
		}
		return stop
	}
	waitForPeers := func(want int) {
		t.Helper()
		for {
			relay.mu.Lock()
			actual := len(relay.peers)
			relay.mu.Unlock()
			if actual == want {
				return
			}
			select {
			case <-ctx.Done():
				t.Fatalf("expected %d connected gateways, got %d", want, actual)
			case <-time.After(20 * time.Millisecond):
			}
		}
	}

	stop := start()
	defer func() {
		stop()
		agents.Wait()
	}()
	waitForPeers(50)
	var clients sync.WaitGroup
	for _, binding := range bindings {
		clients.Add(1)
		go func() {
			defer clients.Done()
			path := prefix + "/gateway/" + binding.ID
			body := []byte(`{"provider":"muse"}`)
			req, _ := http.NewRequestWithContext(ctx, http.MethodPost, public.URL+path+"/pair/start?encoded=a%2Fb", bytes.NewReader(body))
			req.Header.Set("X-XPhone-Relay-Key", binding.Key)
			response, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Error(err)
				return
			}
			data, _ := io.ReadAll(response.Body)
			response.Body.Close()
			if response.StatusCode != http.StatusOK || !bytes.Equal(data, body) || response.Header.Get("X-Test-Query") != "encoded=a%2Fb" {
				t.Errorf("HTTP proxy returned %d with changed data", response.StatusCode)
				return
			}
			headers := make(http.Header)
			headers.Set("X-XPhone-Relay-Key", binding.Key)
			c, _, err := websocket.Dial(ctx, publicWS+path+"/client/ws?account="+binding.ID, &websocket.DialOptions{HTTPHeader: headers})
			if err != nil {
				t.Error(err)
				return
			}
			defer c.CloseNow()
			if err := c.Write(ctx, websocket.MessageBinary, []byte("message")); err != nil {
				t.Error(err)
				return
			}
			kind, message, err := c.Read(ctx)
			if err != nil || kind != websocket.MessageBinary || string(message) != binding.ID+":message" {
				t.Errorf("WebSocket forwarding did not preserve the session: %v", err)
			}
		}()
	}
	clients.Wait()

	wrong, _ := http.NewRequestWithContext(ctx, http.MethodGet, public.URL+prefix+"/gateway/"+bindings[1].ID+"/pair/start", nil)
	wrong.Header.Set("X-XPhone-Relay-Key", bindings[0].Key)
	response, err := http.DefaultClient.Do(wrong)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong gateway credentials returned %d", response.StatusCode)
	}

	stop()
	agents.Wait()
	waitForPeers(0)
	stop = start()
	waitForPeers(50)
}
