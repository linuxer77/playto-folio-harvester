package api

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/go-chi/chi/v5"
	"golang.org/x/oauth2"
)

const (
	googleDriveScope = "https://www.googleapis.com/auth/drive"
	googleAuthTTL    = 10 * time.Minute
)

type pendingGoogleAuth struct {
	CodeVerifier string
	ExpiresAt    time.Time
}

type GoogleAuthHandler struct {
	clientSecretPath string
	tokenPath        string
	redirectURL      string

	mu      sync.Mutex
	pending map[string]pendingGoogleAuth
}

type googleOAuthClientSecretFile struct {
	Installed googleOAuthInstalledClient `json:"installed"`
}

type googleOAuthInstalledClient struct {
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
	AuthURI      string `json:"auth_uri"`
	TokenURI     string `json:"token_uri"`
}

type authorizedUserTokenFile struct {
	ClientID     string   `json:"client_id"`
	ClientSecret string   `json:"client_secret"`
	RefreshToken string   `json:"refresh_token"`
	TokenURI     string   `json:"token_uri"`
	AccessToken  string   `json:"token,omitempty"`
	Scopes       []string `json:"scopes,omitempty"`
	Type         string   `json:"type"`
	Expiry       string   `json:"expiry,omitempty"`
}

func NewGoogleAuthHandler(clientSecretPath, tokenPath, redirectURL string) *GoogleAuthHandler {
	return &GoogleAuthHandler{
		clientSecretPath: clientSecretPath,
		tokenPath:        tokenPath,
		redirectURL:      strings.TrimSpace(redirectURL),
		pending:          make(map[string]pendingGoogleAuth),
	}
}

func (h *GoogleAuthHandler) RegisterRoutes(router chi.Router) {
	router.Get("/api/auth/google/start", h.startGoogleAuth)
	router.Get("/api/auth/google/callback", h.completeGoogleAuth)
}

func (h *GoogleAuthHandler) startGoogleAuth(w http.ResponseWriter, r *http.Request) {
	config, err := h.loadOAuthConfig()
	if err != nil {
		log.Printf("endpoint=GET /api/auth/google/start event=config_load_failed error=%q", err)
		writeError(w, http.StatusInternalServerError, "failed to load Google OAuth client config")
		return
	}

	state, err := randomToken(32)
	if err != nil {
		log.Printf("endpoint=GET /api/auth/google/start event=state_generation_failed error=%q", err)
		writeError(w, http.StatusInternalServerError, "failed to initialize auth flow")
		return
	}

	codeVerifier, err := randomToken(64)
	if err != nil {
		log.Printf("endpoint=GET /api/auth/google/start event=pkce_generation_failed error=%q", err)
		writeError(w, http.StatusInternalServerError, "failed to initialize auth flow")
		return
	}

	h.storePendingState(state, codeVerifier)
	authURL := config.AuthCodeURL(
		state,
		oauth2.AccessTypeOffline,
		oauth2.SetAuthURLParam("prompt", "consent"),
		oauth2.SetAuthURLParam("code_challenge", pkceChallengeS256(codeVerifier)),
		oauth2.SetAuthURLParam("code_challenge_method", "S256"),
	)

	log.Printf("endpoint=GET /api/auth/google/start event=redirect_to_google redirect_url=%q", config.RedirectURL)
	http.Redirect(w, r, authURL, http.StatusFound)
}

func (h *GoogleAuthHandler) completeGoogleAuth(w http.ResponseWriter, r *http.Request) {
	if providerError := strings.TrimSpace(r.URL.Query().Get("error")); providerError != "" {
		renderAuthPopupResponse(w, false, fmt.Sprintf("Google authorization failed: %s", providerError))
		return
	}

	state := strings.TrimSpace(r.URL.Query().Get("state"))
	code := strings.TrimSpace(r.URL.Query().Get("code"))
	if state == "" || code == "" {
		renderAuthPopupResponse(w, false, "Missing OAuth callback state or code.")
		return
	}

	pending, ok := h.consumePendingState(state)
	if !ok {
		renderAuthPopupResponse(w, false, "OAuth session expired. Please try reconnecting again.")
		return
	}

	config, err := h.loadOAuthConfig()
	if err != nil {
		log.Printf("endpoint=GET /api/auth/google/callback event=config_load_failed error=%q", err)
		renderAuthPopupResponse(w, false, "Failed to load OAuth configuration.")
		return
	}

	token, err := config.Exchange(
		r.Context(),
		code,
		oauth2.SetAuthURLParam("code_verifier", pending.CodeVerifier),
	)
	if err != nil {
		log.Printf("endpoint=GET /api/auth/google/callback event=token_exchange_failed error=%q", err)
		renderAuthPopupResponse(w, false, "Failed to exchange OAuth code for a token.")
		return
	}

	if strings.TrimSpace(token.RefreshToken) == "" {
		token.RefreshToken = h.readExistingRefreshToken()
	}
	if strings.TrimSpace(token.RefreshToken) == "" {
		renderAuthPopupResponse(w, false, "Google did not return a refresh token. Revoke app access and try again.")
		return
	}

	if err := h.writeTokenFile(config, token); err != nil {
		log.Printf("endpoint=GET /api/auth/google/callback event=token_write_failed error=%q", err)
		renderAuthPopupResponse(w, false, "Failed to save token.json on backend.")
		return
	}

	log.Printf("endpoint=GET /api/auth/google/callback event=token_saved token_path=%q", h.tokenPath)
	renderAuthPopupResponse(w, true, "Google Drive connected. You can submit jobs again.")
}

func (h *GoogleAuthHandler) loadOAuthConfig() (*oauth2.Config, error) {
	if strings.TrimSpace(h.redirectURL) == "" {
		return nil, fmt.Errorf("GOOGLE_OAUTH_REDIRECT_URL is required")
	}

	raw, err := os.ReadFile(h.clientSecretPath)
	if err != nil {
		return nil, fmt.Errorf("read client secret file: %w", err)
	}

	var payload googleOAuthClientSecretFile
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("parse client secret file: %w", err)
	}

	installed := payload.Installed
	if strings.TrimSpace(installed.ClientID) == "" || strings.TrimSpace(installed.ClientSecret) == "" {
		return nil, fmt.Errorf("client secret file missing client_id/client_secret")
	}
	if strings.TrimSpace(installed.AuthURI) == "" || strings.TrimSpace(installed.TokenURI) == "" {
		return nil, fmt.Errorf("client secret file missing auth_uri/token_uri")
	}

	return &oauth2.Config{
		ClientID:     installed.ClientID,
		ClientSecret: installed.ClientSecret,
		RedirectURL:  h.redirectURL,
		Scopes:       []string{googleDriveScope},
		Endpoint: oauth2.Endpoint{
			AuthURL:  installed.AuthURI,
			TokenURL: installed.TokenURI,
		},
	}, nil
}

func (h *GoogleAuthHandler) writeTokenFile(config *oauth2.Config, token *oauth2.Token) error {
	payload := authorizedUserTokenFile{
		ClientID:     config.ClientID,
		ClientSecret: config.ClientSecret,
		RefreshToken: strings.TrimSpace(token.RefreshToken),
		TokenURI:     config.Endpoint.TokenURL,
		AccessToken:  strings.TrimSpace(token.AccessToken),
		Scopes:       append([]string(nil), config.Scopes...),
		Type:         "authorized_user",
	}
	if !token.Expiry.IsZero() {
		payload.Expiry = token.Expiry.UTC().Format(time.RFC3339Nano)
	}

	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal token payload: %w", err)
	}

	tokenDir := filepath.Dir(h.tokenPath)
	if err := os.MkdirAll(tokenDir, 0o755); err != nil {
		return fmt.Errorf("create token directory: %w", err)
	}

	if err := os.WriteFile(h.tokenPath, data, 0o600); err != nil {
		return fmt.Errorf("write token file: %w", err)
	}

	return nil
}

func (h *GoogleAuthHandler) readExistingRefreshToken() string {
	raw, err := os.ReadFile(h.tokenPath)
	if err != nil {
		return ""
	}

	var payload authorizedUserTokenFile
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}

	return strings.TrimSpace(payload.RefreshToken)
}

func (h *GoogleAuthHandler) storePendingState(state, codeVerifier string) {
	h.mu.Lock()
	defer h.mu.Unlock()

	h.cleanupExpiredLocked(time.Now())
	h.pending[state] = pendingGoogleAuth{
		CodeVerifier: codeVerifier,
		ExpiresAt:    time.Now().Add(googleAuthTTL),
	}
}

func (h *GoogleAuthHandler) consumePendingState(state string) (pendingGoogleAuth, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()

	h.cleanupExpiredLocked(time.Now())
	pending, ok := h.pending[state]
	if !ok {
		return pendingGoogleAuth{}, false
	}
	delete(h.pending, state)

	if pending.ExpiresAt.Before(time.Now()) {
		return pendingGoogleAuth{}, false
	}

	return pending, true
}

func (h *GoogleAuthHandler) cleanupExpiredLocked(now time.Time) {
	for key, value := range h.pending {
		if value.ExpiresAt.Before(now) {
			delete(h.pending, key)
		}
	}
}

func randomToken(byteLength int) (string, error) {
	b := make([]byte, byteLength)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("read random bytes: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func pkceChallengeS256(verifier string) string {
	hash := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(hash[:])
}

func renderAuthPopupResponse(w http.ResponseWriter, success bool, message string) {
	eventType := "google-auth-error"
	title := "Google authorization failed"
	if success {
		eventType = "google-auth-success"
		title = "Google authorization complete"
	}

	payload, _ := json.Marshal(map[string]string{
		"type":    eventType,
		"message": message,
	})

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = fmt.Fprintf(
		w,
		`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>%s</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }
    main { max-width: 28rem; margin: 3rem auto; background: white; border: 1px solid #e2e8f0; border-radius: 0.75rem; padding: 1.25rem; }
    h1 { margin: 0 0 0.5rem; font-size: 1.1rem; }
    p { margin: 0; color: #334155; }
    button { margin-top: 1rem; border: 0; border-radius: 0.5rem; padding: 0.5rem 0.75rem; background: #0f172a; color: white; cursor: pointer; }
  </style>
</head>
<body>
  <main>
    <h1>%s</h1>
    <p>%s</p>
    <button id="close-btn" type="button">Close</button>
  </main>
  <script>
    (function () {
      const payload = %s;
      if (window.opener && window.opener !== window) {
        window.opener.postMessage(payload, window.location.origin);
      }

      const closeBtn = document.getElementById("close-btn");
      if (closeBtn) {
        closeBtn.addEventListener("click", function () {
          window.close();
        });
      }

      if (payload.type === "google-auth-success") {
        setTimeout(function () {
          window.close();
        }, 1200);
      }
    })();
  </script>
</body>
</html>`,
		html.EscapeString(title),
		html.EscapeString(title),
		html.EscapeString(message),
		string(payload),
	)
}
