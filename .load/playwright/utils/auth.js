// Shared auth helper for the browser load VUs.
//
// The server-rendered UI authenticates with the same JWT as the JSON API,
// stored in the HttpOnly `lm_token` cookie. Playwright starts each context
// with no session, so routes would redirect to /login and measure the login
// page instead of the real views.
//
// ensureAuth() logs in (and registers the load account on first run) and
// returns the token. The spec sets it as a cookie for page navigations and
// sends `Authorization: Bearer <token>` for direct API calls.

export const TOKEN_COOKIE = 'lm_token'

const DEFAULT_EMAIL = 'load@example.com'
const DEFAULT_PASSWORD = 'load-password'

export function credentials() {
  return {
    email: process.env.LM_EMAIL ?? DEFAULT_EMAIL,
    password: process.env.LM_PASSWORD ?? DEFAULT_PASSWORD,
  }
}

async function readToken(res) {
  if (!res.ok()) {
    const body = await res.text().catch(() => '')
    throw new Error(`auth failed (${res.status()}): ${body}`)
  }
  const payload = await res.json()
  if (!payload?.token) throw new Error('auth response missing token')
  return payload.token
}

/** Log in, registering the load account if it does not exist yet. */
export async function ensureAuth(request) {
  const data = credentials()
  let res = await request.post('/api/auth/login', { data })
  if (res.status() === 401) {
    res = await request.post('/api/auth/register', { data })
    if (!res.ok()) {
      throw new Error(
        `could not register ${data.email} (${res.status()}). ` +
          'Set LM_EMAIL/LM_PASSWORD to an existing account.',
      )
    }
  }
  return readToken(res)
}

export function authHeaders(token) {
  return { Authorization: `Bearer ${token}` }
}
