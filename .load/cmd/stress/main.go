// LifeManager API stress tool — Go + fasthttp.
//
// Why fasthttp: zero-alloc request/response pooling (AcquireRequest/Release),
// MaxConnsPerHost tuning, and ~10x less GC pressure than net/http at high
// concurrency. This matters when you push 500+ concurrent VUs at a
// Python/uvicorn backend backed by SQLite (aiosqlite serializes writes).
//
// Usage:
//
//	go run ./cmd/stress -base-url http://127.0.0.1:8000 -c 50 -n 10000
//	go run ./cmd/stress -base-url http://127.0.0.1:8000 -c 100 -duration 60s -ramp 10s -out results.json
//
// The JSON API requires a JWT except for /api/health and /api/auth/*. The
// tool logs in with -email/-password (or -token) and sends the bearer header
// on every request; if the account does not exist yet it registers it (the
// first account becomes admin).
//
// Workload mix mirrors backend/app/web.py plus the write paths from
// backend/smoke_test.py. Weights sum to 100:
//
//	health              5%   GET /api/health
//	today              30%   GET /api/home/today            (heaviest: TodayService)
//	list-tasks         15%   GET /api/tasks
//	list-projects      10%   GET /api/projects
//	list-routines      10%   GET /api/routines
//	heatmap            10%   GET /api/stats/heatmap?type=tasks
//	calendar            5%   GET /api/stats/calendar?type=tasks&year=..&month=..
//	write-task         10%   POST /api/tasks -> POST /complete -> DELETE (self-cleaning)
//	write-project       5%   POST /api/projects -> POST /{id}/tasks -> DELETE project
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/valyala/fasthttp"
)

type Config struct {
	BaseURL  string
	Conns    int
	Total    int64
	Duration time.Duration
	Ramp     time.Duration
	Timeout  time.Duration
	Out      string
	Email    string
	Password string
	Token    string
	WritePct int // reserved for future mix override; currently baked into weights
}

type endpointStat struct {
	Name   string  `json:"name"`
	Count  int64   `json:"count"`
	Errors int64   `json:"errors"`
	MeanMs float64 `json:"mean_ms"`
	P50Ms  float64 `json:"p50_ms"`
	P95Ms  float64 `json:"p95_ms"`
	P99Ms  float64 `json:"p99_ms"`
	MaxMs  float64 `json:"max_ms"`
}

type summary struct {
	BaseURL      string           `json:"base_url"`
	Concurrency  int              `json:"concurrency"`
	Total        int64            `json:"total"`
	Success      int64            `json:"success"`
	Errors       int64            `json:"errors"`
	ErrorsByCode map[string]int64 `json:"errors_by_code"`
	ElapsedS     float64          `json:"elapsed_s"`
	ReqPerSec    float64          `json:"req_per_sec"`
	MeanMs       float64          `json:"mean_ms"`
	P50Ms        float64          `json:"p50_ms"`
	P95Ms        float64          `json:"p95_ms"`
	P99Ms        float64          `json:"p99_ms"`
	MaxMs        float64          `json:"max_ms"`
	Endpoints    []endpointStat   `json:"endpoints"`
}

// weighted scenario picker
type scenario struct {
	name   string
	weight int
	fn     func(w *worker) (status int, err error)
}

func main() {
	var cfg Config
	var durationStr, rampStr string
	flag.StringVar(&cfg.BaseURL, "base-url", "http://127.0.0.1:8000", "backend base URL")
	flag.IntVar(&cfg.Conns, "c", 50, "concurrent workers")
	flag.Int64Var(&cfg.Total, "n", 10000, "total requests (ignored when -duration is set)")
	flag.StringVar(&durationStr, "duration", "", "soak duration, e.g. 60s (overrides -n)")
	flag.StringVar(&rampStr, "ramp", "0s", "linear ramp-up, e.g. 10s")
	flag.DurationVar(&cfg.Timeout, "timeout", 10*time.Second, "per-request timeout")
	flag.StringVar(&cfg.Out, "out", "", "write JSON summary to file")
	flag.StringVar(&cfg.Email, "email", envOr("LM_EMAIL", "load@example.com"), "login email (registers if missing)")
	flag.StringVar(&cfg.Password, "password", envOr("LM_PASSWORD", "load-password"), "login password")
	flag.StringVar(&cfg.Token, "token", os.Getenv("LM_TOKEN"), "bearer token (skips login when set)")
	flag.Parse()

	if durationStr != "" {
		d, err := time.ParseDuration(durationStr)
		if err != nil {
			fatalf("bad -duration: %v", err)
		}
		cfg.Duration = d
	}
	if rampStr != "" {
		d, err := time.ParseDuration(rampStr)
		if err != nil {
			fatalf("bad -ramp: %v", err)
		}
		cfg.Ramp = d
	}

	client := &fasthttp.Client{
		MaxConnsPerHost:     cfg.Conns*2 + 16,
		ReadTimeout:         cfg.Timeout,
		WriteTimeout:        cfg.Timeout,
		MaxIdleConnDuration: 30 * time.Second,
	}

	if cfg.Token == "" {
		token, err := (&worker{cfg: &cfg, client: client}).authenticate()
		if err != nil {
			fatalf("%v", err)
		}
		cfg.Token = token
	}

	scenarios := buildScenarios(&cfg)

	var (
		issued    atomic.Int64
		done      atomic.Int64
		success   atomic.Int64
		errors    atomic.Int64
		errByCode sync.Map // string -> *atomic.Int64
		mu        sync.Mutex
		latAll    = make([]float64, 0, 1024)
		latByEP   = map[string][]float64{}
		stop      = make(chan struct{})
	)

	if cfg.Duration > 0 {
		go func() {
			time.Sleep(cfg.Duration)
			close(stop)
		}()
	}

	record := func(ep string, ms float64, ok bool, code string) {
		mu.Lock()
		latAll = append(latAll, ms)
		latByEP[ep] = append(latByEP[ep], ms)
		mu.Unlock()
		if ok {
			success.Add(1)
		} else {
			errors.Add(1)
			v, _ := errByCode.LoadOrStore(code, &atomic.Int64{})
			v.(*atomic.Int64).Add(1)
		}
		done.Add(1)
	}

	start := time.Now()
	var wg sync.WaitGroup

	// Weighted picker table (cumulative).
	totalW := 0
	for _, s := range scenarios {
		totalW += s.weight
	}
	pick := func(r *rand.Rand) *scenario {
		x := r.Intn(totalW)
		for i := range scenarios {
			x -= scenarios[i].weight
			if x < 0 {
				return &scenarios[i]
			}
		}
		return &scenarios[0]
	}

	for i := 0; i < cfg.Conns; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			// linear ramp: stagger worker start
			if cfg.Ramp > 0 {
				delay := time.Duration(int64(cfg.Ramp) * int64(id) / int64(cfg.Conns))
				select {
				case <-time.After(delay):
				case <-stop:
					return
				}
			}
			r := rand.New(rand.NewSource(time.Now().UnixNano() + int64(id)*7919))
			w := &worker{cfg: &cfg, client: client, id: id}
			for {
				select {
				case <-stop:
					return
				default:
				}
				if cfg.Duration == 0 && issued.Add(1) > cfg.Total {
					return
				}
				s := pick(r)
				t0 := time.Now()
				status, err := s.fn(w)
				ms := float64(time.Since(t0).Microseconds()) / 1000.0
				ok := err == nil && status >= 200 && status < 400
				code := fmt.Sprintf("%d", status)
				if err != nil {
					code = "ERR:" + errClass(err)
				}
				record(s.name, ms, ok, code)
			}
		}(i)
	}
	wg.Wait()
	elapsed := time.Since(start)

	mu.Lock()
	defer mu.Unlock()
	sum := summary{
		BaseURL:      cfg.BaseURL,
		Concurrency:  cfg.Conns,
		Total:        done.Load(),
		Success:      success.Load(),
		Errors:       errors.Load(),
		ErrorsByCode: map[string]int64{},
		ElapsedS:     elapsed.Seconds(),
	}
	errByCode.Range(func(k, v any) bool {
		sum.ErrorsByCode[k.(string)] = v.(*atomic.Int64).Load()
		return true
	})
	if len(latAll) > 0 {
		mean, p50, p95, p99, mx := quantiles(latAll)
		sum.MeanMs, sum.P50Ms, sum.P95Ms, sum.P99Ms, sum.MaxMs = mean, p50, p95, p99, mx
		sum.ReqPerSec = float64(len(latAll)) / elapsed.Seconds()
	}
	for _, s := range scenarios {
		l := latByEP[s.name]
		if len(l) == 0 {
			continue
		}
		mean, p50, p95, p99, mx := quantiles(l)
		var ec int64
		sum.Endpoints = append(sum.Endpoints, endpointStat{
			Name: s.name, Count: int64(len(l)), Errors: ec,
			MeanMs: mean, P50Ms: p50, P95Ms: p95, P99Ms: p99, MaxMs: mx,
		})
	}
	sort.Slice(sum.Endpoints, func(i, j int) bool { return sum.Endpoints[i].Name < sum.Endpoints[j].Name })

	printHuman(&sum)
	if cfg.Out != "" {
		b, _ := json.MarshalIndent(sum, "", "  ")
		if err := os.WriteFile(cfg.Out, b, 0644); err != nil {
			fatalf("write out: %v", err)
		}
		fmt.Printf("wrote %s\n", cfg.Out)
	}
	if sum.Errors > 0 && sum.Success == 0 {
		os.Exit(2)
	}
}

type worker struct {
	cfg    *Config
	client *fasthttp.Client
	id     int
}

// authenticate logs in with the configured credentials, registering the
// account first if it does not exist yet.
func (w *worker) authenticate() (string, error) {
	creds, _ := json.Marshal(map[string]string{
		"email":    w.cfg.Email,
		"password": w.cfg.Password,
	})
	status, body, err := w.doJSON("POST", "/api/auth/login", creds)
	if err != nil {
		return "", fmt.Errorf("login: %w", err)
	}
	if status == fasthttp.StatusUnauthorized {
		status, body, err = w.doJSON("POST", "/api/auth/register", creds)
		if err != nil {
			return "", fmt.Errorf("register: %w", err)
		}
		if status != fasthttp.StatusOK {
			return "", fmt.Errorf("register %s failed (%d): %s — set -email/-password or -token", w.cfg.Email, status, body)
		}
	} else if status != fasthttp.StatusOK {
		return "", fmt.Errorf("login %s failed (%d): %s — set -email/-password or -token", w.cfg.Email, status, body)
	}
	var payload struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(body, &payload); err != nil || payload.Token == "" {
		return "", fmt.Errorf("auth response missing token: %s", body)
	}
	return payload.Token, nil
}

func (w *worker) do(method, path string, body []byte) (int, error) {
	req := fasthttp.AcquireRequest()
	resp := fasthttp.AcquireResponse()
	defer fasthttp.ReleaseRequest(req)
	defer fasthttp.ReleaseResponse(resp)
	req.SetRequestURI(w.cfg.BaseURL + path)
	req.Header.SetMethod(method)
	if w.cfg.Token != "" {
		req.Header.Set("Authorization", "Bearer "+w.cfg.Token)
	}
	if body != nil {
		req.Header.SetContentType("application/json")
		req.SetBody(body)
	}
	if err := w.client.Do(req, resp); err != nil {
		return 0, err
	}
	return resp.StatusCode(), nil
}

func (w *worker) doJSON(method, path string, body []byte) (int, []byte, error) {
	req := fasthttp.AcquireRequest()
	resp := fasthttp.AcquireResponse()
	defer fasthttp.ReleaseRequest(req)
	defer fasthttp.ReleaseResponse(resp)
	req.SetRequestURI(w.cfg.BaseURL + path)
	req.Header.SetMethod(method)
	if w.cfg.Token != "" {
		req.Header.Set("Authorization", "Bearer "+w.cfg.Token)
	}
	if body != nil {
		req.Header.SetContentType("application/json")
		req.SetBody(body)
	}
	if err := w.client.Do(req, resp); err != nil {
		return 0, nil, err
	}
	b := append([]byte(nil), resp.Body()...)
	return resp.StatusCode(), b, nil
}

func buildScenarios(cfg *Config) []scenario {
	now := time.Now()
	calPath := fmt.Sprintf("/api/stats/calendar?type=tasks&year=%d&month=%d", now.Year(), int(now.Month()))
	return []scenario{
		{name: "health", weight: 5, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/health", nil)
		}},
		{name: "today", weight: 30, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/home/today", nil)
		}},
		{name: "list-tasks", weight: 15, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/tasks", nil)
		}},
		{name: "list-projects", weight: 10, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/projects", nil)
		}},
		{name: "list-routines", weight: 10, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/routines", nil)
		}},
		{name: "heatmap", weight: 10, fn: func(w *worker) (int, error) {
			return w.do("GET", "/api/stats/heatmap?type=tasks", nil)
		}},
		{name: "calendar", weight: 5, fn: func(w *worker) (int, error) {
			return w.do("GET", calPath, nil)
		}},
		{name: "write-task", weight: 10, fn: func(w *worker) (int, error) {
			body := []byte(fmt.Sprintf(`{"title":"load-%d-%d"}`, w.id, time.Now().UnixNano()))
			st, respBody, err := w.doJSON("POST", "/api/tasks", body)
			if err != nil || st != 200 {
				return st, errOr(st, err)
			}
			var created struct {
				ID int `json:"id"`
			}
			if err := json.Unmarshal(respBody, &created); err != nil || created.ID == 0 {
				return st, fmt.Errorf("decode")
			}
			st, err = w.do("POST", fmt.Sprintf("/api/tasks/%d/complete", created.ID), []byte(`{}`))
			if err != nil || st != 200 {
				return st, errOr(st, err)
			}
			st, err = w.do("DELETE", fmt.Sprintf("/api/tasks/%d", created.ID), nil)
			return st, err
		}},
		{name: "write-project", weight: 5, fn: func(w *worker) (int, error) {
			body := []byte(fmt.Sprintf(`{"name":"load-proj-%d-%d"}`, w.id, time.Now().UnixNano()))
			st, respBody, err := w.doJSON("POST", "/api/projects", body)
			if err != nil || st != 200 {
				return st, errOr(st, err)
			}
			var created struct {
				ID int `json:"id"`
			}
			if err := json.Unmarshal(respBody, &created); err != nil || created.ID == 0 {
				return st, fmt.Errorf("decode")
			}
			st, err = w.do("POST", fmt.Sprintf("/api/projects/%d/tasks", created.ID), []byte(`{"title":"sub"}`))
			if err != nil || st != 200 {
				return st, errOr(st, err)
			}
			st, err = w.do("DELETE", fmt.Sprintf("/api/projects/%d", created.ID), nil)
			return st, err
		}},
	}
}

// --- helpers ---

func quantiles(ms []float64) (mean, p50, p95, p99, mx float64) {
	cp := append([]float64(nil), ms...)
	sort.Float64s(cp)
	var sum float64
	for _, v := range cp {
		sum += v
	}
	mean = sum / float64(len(cp))
	q := func(p float64) float64 {
		i := int(p * float64(len(cp)-1))
		return cp[i]
	}
	return mean, q(0.50), q(0.95), q(0.99), cp[len(cp)-1]
}

func errClass(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, os.ErrDeadlineExceeded) || strings.Contains(strings.ToLower(err.Error()), "timeout") {
		return "timeout"
	}
	return "conn"
}

func errOr(status int, err error) error {
	if err != nil {
		return err
	}
	return fmt.Errorf("status-%d", status)
}

func printHuman(s *summary) {
	fmt.Printf("\n=== LifeManager stress ===\n")
	fmt.Printf("base=%s conc=%d total=%d ok=%d err=%d elapsed=%.1fs rps=%.1f\n",
		s.BaseURL, s.Concurrency, s.Total, s.Success, s.Errors, s.ElapsedS, s.ReqPerSec)
	fmt.Printf("ALL  mean=%.1fms p50=%.1fms p95=%.1fms p99=%.1fms max=%.1fms\n",
		s.MeanMs, s.P50Ms, s.P95Ms, s.P99Ms, s.MaxMs)
	if len(s.ErrorsByCode) > 0 {
		fmt.Printf("errors: %v\n", s.ErrorsByCode)
	}
	fmt.Printf("\n%-14s %8s %8s %8s %8s %8s\n", "endpoint", "mean", "p50", "p95", "p99", "max")
	for _, e := range s.Endpoints {
		fmt.Printf("%-14s %7.1fms %7.1fms %7.1fms %7.1fms %7.1fms  (n=%d)\n",
			e.Name, e.MeanMs, e.P50Ms, e.P95Ms, e.P99Ms, e.MaxMs, e.Count)
	}
	fmt.Println("\nBottleneck hints: today/heatmap/calendar are compute-heavy (TodayService/HeatmapService);")
	fmt.Println("write-* contend on SQLite (aiosqlite serializes writes) — p95 spike there = DB lock, not CPU.")
}

func fatalf(f string, a ...any) {
	fmt.Printf("FATAL: "+f+"\n", a...)
	os.Exit(1)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
