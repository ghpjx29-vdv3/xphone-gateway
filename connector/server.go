package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/hashicorp/yamux"
)

var (
	gatewayID = regexp.MustCompile(`^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$`)
	relayKey  = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
)

type peer struct {
	key     string
	session *yamux.Session
	proxy   *httputil.ReverseProxy
}

type relay struct {
	accounts string
	key      string
	client   *http.Client
	mu       sync.Mutex
	peers    map[string]*peer
	gates    [32]sync.Mutex
}

func newRelay(accounts, key string) *relay {
	return &relay{
		accounts: accounts, key: key, peers: make(map[string]*peer),
		client: &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
	}
}

func (relay *relay) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+prefix+"/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = io.WriteString(w, `{"ok":true,"service":"xphone-vps-relay"}`)
	})
	mux.HandleFunc("GET "+prefix+"/connect", relay.connect)
	mux.HandleFunc(prefix+"/gateway/{id}/", relay.forward)
	mux.HandleFunc("POST "+prefix+"/internal/revoke/{id}", relay.revoke)
	return mux
}

func equal(left, right string) bool {
	return subtle.ConstantTimeCompare([]byte(left), []byte(right)) == 1
}

func (relay *relay) gatewayLock(id string) *sync.Mutex {
	hash := fnv.New32a()
	_, _ = io.WriteString(hash, id)
	return &relay.gates[hash.Sum32()%uint32(len(relay.gates))]
}

func (relay *relay) lookup(ctx context.Context, id string) (string, error) {
	body, _ := json.Marshal(map[string]string{"gatewayId": id})
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, relay.accounts, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	request.Header.Set("Authorization", "Bearer "+relay.key)
	request.Header.Set("Content-Type", "application/json")
	response, err := relay.client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return "", nil
	}
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("gateway lookup: HTTP %d", response.StatusCode)
	}
	var binding struct {
		ID  string `json:"gatewayId"`
		Key string `json:"relaySecret"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&binding); err != nil {
		return "", err
	}
	if binding.ID != id || !relayKey.MatchString(binding.Key) {
		return "", fmt.Errorf("gateway lookup returned an invalid binding")
	}
	return binding.Key, nil
}

func (relay *relay) connect(w http.ResponseWriter, r *http.Request) {
	id := r.Header.Get("X-XPhone-Gateway-Id")
	token, bearer := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !gatewayID.MatchString(id) || !bearer || !relayKey.MatchString(token) {
		http.Error(w, "Authentication required", http.StatusUnauthorized)
		return
	}
	gate := relay.gatewayLock(id)
	gate.Lock()
	key, err := relay.lookup(r.Context(), id)
	if err != nil {
		gate.Unlock()
		http.Error(w, "Connection unavailable", http.StatusServiceUnavailable)
		return
	}
	if !equal(token, key) {
		gate.Unlock()
		http.Error(w, "Authentication required", http.StatusUnauthorized)
		return
	}
	socket, err := websocket.Accept(w, r, nil)
	if err != nil {
		gate.Unlock()
		return
	}
	conn := websocket.NetConn(r.Context(), socket, websocket.MessageBinary)
	defer conn.Close()
	session, err := yamux.Client(conn, sessionConfig())
	if err != nil {
		gate.Unlock()
		return
	}
	defer session.Close()

	current := &peer{key: key, session: session, proxy: newProxy(session)}
	relay.mu.Lock()
	previous := relay.peers[id]
	relay.peers[id] = current
	relay.mu.Unlock()
	gate.Unlock()
	if previous != nil {
		_ = previous.session.Close()
	}

	<-session.CloseChan()
	relay.mu.Lock()
	if relay.peers[id] == current {
		delete(relay.peers, id)
	}
	relay.mu.Unlock()
	current.proxy.Transport.(*http.Transport).CloseIdleConnections()
}

func newProxy(session *yamux.Session) *httputil.ReverseProxy {
	target := &url.URL{Scheme: "http", Host: runtimeAddr}
	return &httputil.ReverseProxy{
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(target)
			// The local runtime verifies the original path and query against the signed route.
			request.Out.URL.RawQuery = request.In.URL.RawQuery
		},
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				if err := ctx.Err(); err != nil {
					return nil, err
				}
				return session.OpenStream()
			},
			MaxIdleConnsPerHost: 4, IdleConnTimeout: 45 * time.Second, ResponseHeaderTimeout: 60 * time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "Connection unavailable", http.StatusServiceUnavailable)
		},
	}
}

func (relay *relay) forward(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !gatewayID.MatchString(id) {
		http.NotFound(w, r)
		return
	}
	relay.mu.Lock()
	current := relay.peers[id]
	relay.mu.Unlock()
	if current == nil {
		http.Error(w, "Connection unavailable", http.StatusServiceUnavailable)
		return
	}
	if !equal(r.Header.Get("X-XPhone-Relay-Key"), current.key) {
		http.Error(w, "Authentication required", http.StatusUnauthorized)
		return
	}
	routePrefix := prefix + "/gateway/" + id
	r.URL.Path = strings.TrimPrefix(r.URL.Path, routePrefix)
	r.URL.RawPath = strings.TrimPrefix(r.URL.RawPath, routePrefix)
	current.proxy.ServeHTTP(w, r)
}

func (relay *relay) revoke(w http.ResponseWriter, r *http.Request) {
	if !equal(r.Header.Get("Authorization"), "Bearer "+relay.key) {
		http.Error(w, "Authentication required", http.StatusUnauthorized)
		return
	}
	gate := relay.gatewayLock(r.PathValue("id"))
	gate.Lock()
	defer gate.Unlock()
	relay.mu.Lock()
	current := relay.peers[r.PathValue("id")]
	delete(relay.peers, r.PathValue("id"))
	relay.mu.Unlock()
	if current != nil {
		_ = current.session.Close()
	}
	w.WriteHeader(http.StatusNoContent)
}

func (relay *relay) close() {
	relay.mu.Lock()
	defer relay.mu.Unlock()
	for id, current := range relay.peers {
		delete(relay.peers, id)
		_ = current.session.Close()
	}
}
