import { DurableObject } from "cloudflare:workers";

/**
 * Worker that fronts the LifeManager Docker container (Python/FastAPI).
 *
 * Cloudflare Containers are managed through a Durable Object: on the first
 * request the DO boots the container image, waits for the app's health
 * endpoint, then forwards every request to the container's port 8080.
 *
 * The app is stateless - all state lives in Cloudflare D1 - so a single
 * named Durable Object instance ("singleton") owns one shared container.
 *
 * The container reaches D1 through the Cloudflare REST API
 * (backend/app/d1.py), so the account/database IDs and an API token are
 * passed in as environment variables at container start.
 */

interface Env {
  /** Durable Object namespace that owns the container. */
  CONTAINER: DurableObjectNamespace;
  /** D1 binding (kept for wrangler d1 commands / future Worker-side queries). */
  DB: D1Database;

  // Non-secret vars (wrangler.jsonc).
  CLOUDFLARE_ACCOUNT_ID: string;
  D1_DATABASE_ID: string;
  ALLOW_SIGNUP?: string;
  ADMIN_EMAIL?: string;

  // Secrets (npx wrangler secret put ...).
  CLOUDFLARE_API_TOKEN: string;
  JWT_SECRET: string;
  ADMIN_PASSWORD?: string;
}

const PORT = 8080;
const INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000; // keep warm 30 min after last request
const CONTAINER_NAME = "singleton";
const READY_ATTEMPTS = 150; // 150 * 200ms ≈ 30s startup budget
const READY_PROBE_URL = "http://container/api/health";

export class LifeManagerContainer extends DurableObject<Env> {
  private starting: Promise<void> | undefined;
  private monitoring: Promise<void> | undefined;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    if (ctx.container?.running) {
      // Restore the timeout and exit monitoring after a DO restart.
      void ctx.blockConcurrencyWhile(() =>
        ctx.container!.setInactivityTimeout(INACTIVITY_TIMEOUT_MS),
      );
      this.observeExit();
    }
  }

  async fetch(request: Request): Promise<Response> {
    // Share startup across concurrent requests, including after a failed attempt.
    this.starting ??= this.startAndWaitForPort().finally(() => {
      this.starting = undefined;
    });
    await this.starting;

    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    return this.ctx.container!.getTcpPort(PORT).fetch(forwarded);
  }

  private async startAndWaitForPort(): Promise<void> {
    const container = this.ctx.container!;
    if (!container.running) {
      // Scheduling policy is "default" (wrangler.jsonc), so image and instance
      // type come from configuration; only internet access and env are set here.
      // Internet access is required: the app talks to the D1 REST API.
      container.start({
        enableInternet: true,
        env: {
          LIFEMANAGER_DB_DRIVER: "d1",
          LIFEMANAGER_D1_ACCOUNT_ID: this.env.CLOUDFLARE_ACCOUNT_ID,
          LIFEMANAGER_D1_DATABASE_ID: this.env.D1_DATABASE_ID,
          LIFEMANAGER_D1_API_TOKEN: this.env.CLOUDFLARE_API_TOKEN,
          LIFEMANAGER_JWT_SECRET: this.env.JWT_SECRET,
          LIFEMANAGER_ALLOW_SIGNUP: this.env.ALLOW_SIGNUP ?? "true",
          ...(this.env.ADMIN_EMAIL ? { LIFEMANAGER_ADMIN_EMAIL: this.env.ADMIN_EMAIL } : {}),
          ...(this.env.ADMIN_PASSWORD ? { LIFEMANAGER_ADMIN_PASSWORD: this.env.ADMIN_PASSWORD } : {}),
        },
      });
    }
    this.observeExit();
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);

    // "running" means startup was requested, not that the HTTP server is ready.
    const port = container.getTcpPort(PORT);
    for (let attempt = 0; attempt < READY_ATTEMPTS; attempt++) {
      try {
        const response = await port.fetch(READY_PROBE_URL, {
          signal: AbortSignal.timeout(1000),
        });
        await response.body?.cancel();
        if (response.ok) return;
      } catch {
        // The port may not be listening yet; retry the readiness probe only.
      }
      await scheduler.wait(200);
    }
    throw new Error("Container did not become ready on port 8080");
  }

  private observeExit(): void {
    if (this.monitoring) return;
    this.monitoring = this.ctx
      .container!.monitor()
      .then(() => console.log("container exited"))
      .catch((error: unknown) => console.error("container failed:", error))
      .finally(() => {
        this.monitoring = undefined;
      });
    this.ctx.waitUntil(this.monitoring);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return env.CONTAINER.getByName(CONTAINER_NAME).fetch(request);
  },
} satisfies ExportedHandler<Env>;
