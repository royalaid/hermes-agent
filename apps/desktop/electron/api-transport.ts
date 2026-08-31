/**
 * Shared HTTP transport policy for the Electron main process's Hermes REST
 * helpers (fetchJson / fetchPublicJson / downloadViaTokenToFile).
 *
 * Two concerns live here so they can be unit-tested without Electron:
 *
 * 1. Connection-pooled keep-alive agents. Opening a fresh TCP socket per call
 *    is what produced the burst-traffic ECONNRESET storms (#92976): the
 *    backend closes idle keep-alive sockets and the next write on a reused
 *    raw socket dies with 'socket hang up'. JSON calls and streaming
 *    downloads get SEPARATE pools so a handful of long-lived download
 *    streams can never starve the small, latency-sensitive JSON calls out of
 *    the socket pool.
 *
 * 2. A retry policy that is safe for non-idempotent verbs. A transient
 *    transport error does NOT mean the server didn't process the request —
 *    an ECONNRESET can arrive after the backend already handled a POST
 *    (created the session, submitted the prompt) and merely lost the socket
 *    before the response was read. Blindly retrying every verb double-submits.
 *
 *    The rule implemented by shouldRetryRequest():
 *      - Idempotent verbs (GET / HEAD / OPTIONS) retry on any transient
 *        transport error — replaying them is harmless by definition.
 *      - Non-idempotent verbs (POST / PUT / PATCH / DELETE) retry ONLY when
 *        the request provably never reached the server:
 *          a) connection-establishment failures (ECONNREFUSED, ENOTFOUND,
 *             EAI_AGAIN, EHOSTUNREACH, ENETUNREACH) — no connection means no
 *             request; or
 *          b) a transient error thrown before we started flushing the
 *             request (requestState.bodySent === false).
 *        Anything ambiguous — ECONNRESET / EPIPE / 'socket hang up' after
 *        the body went out — is NOT retried; the error surfaces to the
 *        caller. When in doubt, don't retry a non-idempotent request.
 */

import http from 'node:http'
import https from 'node:https'

type ByteLimitedResponse = {
  headers: Record<string, string | string[] | undefined>
  on(event: 'error', listener: (error: Error) => void): unknown
  on(event: 'data', listener: (chunk: Buffer | Uint8Array | string) => void): unknown
  on(event: 'end', listener: () => void): unknown
  destroy?: () => void
}

// JSON pool: many small concurrent calls (session lists, config, prompts).
const HTTP_JSON_AGENT = new http.Agent({ keepAlive: true, maxSockets: 50 })
const HTTPS_JSON_AGENT = new https.Agent({ keepAlive: true, maxSockets: 50 })

// Download pool: few long-lived streaming bodies. Isolated from the JSON pool
// so saturating it with large file downloads can't block interactive calls.
const HTTP_DOWNLOAD_AGENT = new http.Agent({ keepAlive: true, maxSockets: 8 })
const HTTPS_DOWNLOAD_AGENT = new https.Agent({ keepAlive: true, maxSockets: 8 })

function jsonAgentFor(protocol) {
  return protocol === 'https:' ? HTTPS_JSON_AGENT : HTTP_JSON_AGENT
}

function downloadAgentFor(protocol) {
  return protocol === 'https:' ? HTTPS_DOWNLOAD_AGENT : HTTP_DOWNLOAD_AGENT
}

// Close pooled sockets so lingering keep-alive connections can't hold the
// process open (or leak FDs) across quit. Wired to app 'will-quit' in main.ts.
function destroyKeepaliveAgents() {
  for (const agent of [HTTP_JSON_AGENT, HTTPS_JSON_AGENT, HTTP_DOWNLOAD_AGENT, HTTPS_DOWNLOAD_AGENT]) {
    agent.destroy()
  }
}

// Transient transport errors: retry MAY be safe (subject to verb gating).
const TRANSIENT_CODES = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'EPIPE',
  'ETIMEDOUT',
  'EAI_AGAIN',
  'ENOTFOUND',
  'EHOSTUNREACH',
  'ENETUNREACH'
])

// Errors that prove the request never reached the server: the TCP connection
// (or name resolution) failed outright, so nothing was submitted.
const NEVER_SENT_CODES = new Set(['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH'])

const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])
const TODO_STATE_TRANSPORT_RESPONSE_BYTES = 1_100_000

class ResponseBodyTooLargeError extends Error {
  code = 'HERMES_RESPONSE_BODY_TOO_LARGE'
  maxBytes: number
  observedBytes: number
  declaredBytes: number | null

  constructor({ maxBytes, observedBytes, declaredBytes = null }) {
    super(`Hermes response exceeded the ${maxBytes}-byte transport limit`)
    this.name = 'ResponseBodyTooLargeError'
    this.maxBytes = maxBytes
    this.observedBytes = observedBytes
    this.declaredBytes = declaredBytes
  }
}

function responseByteLimitForApiRequest(request: any): number | null {
  if (String(request?.method || 'GET').toUpperCase() !== 'GET') {
    return null
  }
  let parsed: URL
  try {
    parsed = new URL(String(request?.path || ''), 'http://hermes.local')
  } catch {
    return null
  }
  if (parsed.searchParams.get('projection') !== 'todo-state') {
    return null
  }
  if (!/^\/api\/sessions\/[^/]+\/messages$/.test(parsed.pathname)) {
    return null
  }
  return TODO_STATE_TRANSPORT_RESPONSE_BYTES
}

function readJsonResponseWithByteLimit(
  response: ByteLimitedResponse,
  { maxBytes, abort }: { maxBytes: number; url?: string; abort?: () => void }
): Promise<string> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) {
    return Promise.reject(new TypeError('maxBytes must be a non-negative safe integer'))
  }

  const rawDeclared = response.headers['content-length']
  const declaredText = Array.isArray(rawDeclared) ? rawDeclared[0] : rawDeclared
  const declaredBytes = typeof declaredText === 'string' && /^\d+$/.test(declaredText) ? Number(declaredText) : null
  if (declaredBytes !== null && Number.isSafeInteger(declaredBytes) && declaredBytes > maxBytes) {
    if (abort) {
      abort()
    } else {
      response.destroy?.()
    }
    return Promise.reject(new ResponseBodyTooLargeError({ maxBytes, observedBytes: 0, declaredBytes }))
  }

  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let observedBytes = 0
    let settled = false

    const settleReject = error => {
      if (settled) {
        return
      }
      settled = true
      reject(error)
    }
    response.on('error', settleReject)
    response.on('data', chunk => {
      if (settled) {
        return
      }
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      observedBytes += bytes.length
      if (observedBytes > maxBytes) {
        settled = true
        if (abort) {
          abort()
        } else {
          response.destroy?.()
        }
        reject(new ResponseBodyTooLargeError({ maxBytes, observedBytes, declaredBytes }))
        return
      }
      chunks.push(bytes)
    })
    response.on('end', () => {
      if (settled) {
        return
      }
      settled = true
      resolve(Buffer.concat(chunks, observedBytes).toString('utf8'))
    })
  })
}

function readApiJsonResponseWithByteLimit(
  response: ByteLimitedResponse,
  { method = 'GET', url, path, abort }: { method?: string; url: string; path?: string; abort?: () => void }
): Promise<string> {
  let requestPath = path
  if (!requestPath) {
    try {
      const parsed = new URL(url)
      requestPath = `${parsed.pathname}${parsed.search}`
    } catch {
      return Promise.reject(new TypeError('url must be an absolute HTTP URL'))
    }
  }

  const maxBytes = responseByteLimitForApiRequest({ method, path: requestPath })

  return readJsonResponseWithByteLimit(response, {
    maxBytes: maxBytes ?? Number.MAX_SAFE_INTEGER,
    url,
    abort
  })
}

function isIdempotentMethod(method) {
  return IDEMPOTENT_METHODS.has(String(method || 'GET').toUpperCase())
}

function isTransientTransportError(error) {
  if (!error) {
    return false
  }

  if (TRANSIENT_CODES.has(error.code)) {
    return true
  }

  const msg = String(error.message || '')

  return msg.includes('socket hang up') || msg.includes('read ECONNRESET')
}

/**
 * The verb-gated retry decision.
 *
 * @param error        the transport error from the failed attempt
 * @param method       HTTP verb of the request ('GET', 'POST', ...)
 * @param requestState per-attempt state; requestState.bodySent is set true by
 *                     the caller just BEFORE the first byte of the request is
 *                     flushed, so a `false` here proves nothing went out.
 */
function shouldRetryRequest(error, method, requestState: any = {}) {
  if (!isTransientTransportError(error)) {
    return false
  }

  if (isIdempotentMethod(method)) {
    return true
  }

  // Non-idempotent: only when the request provably never reached the server.
  if (NEVER_SENT_CODES.has(error && error.code)) {
    return true
  }

  if (requestState.bodySent === false) {
    return true
  }

  // Ambiguous (reset/hang-up after the body was flushed): the server may have
  // processed it. Surface the error rather than risk a double submit.
  return false
}

/**
 * Run `makeAttempt` with bounded retries under the policy above.
 *
 * `makeAttempt(requestState)` must return a Promise and should set
 * `requestState.bodySent = true` immediately before flushing the request
 * (before the first req.write()/req.end()). Each attempt gets a fresh state
 * object initialized to { bodySent: false }.
 */
async function withRetry(makeAttempt, options: any = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const maxRetries = Number.isInteger(options.maxRetries) ? options.maxRetries : 2

  const delayFn =
    options.delayFn || (attempt => new Promise(r => setTimeout(r, Math.min(200 * Math.pow(2, attempt), 2000))))

  let lastError

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const requestState = { bodySent: false }

    try {
      return await makeAttempt(requestState)
    } catch (error) {
      lastError = error

      if (attempt < maxRetries && shouldRetryRequest(error, method, requestState)) {
        await delayFn(attempt)

        continue
      }

      throw error
    }
  }

  throw lastError
}

export {
  destroyKeepaliveAgents,
  downloadAgentFor,
  isIdempotentMethod,
  isTransientTransportError,
  jsonAgentFor,
  readApiJsonResponseWithByteLimit,
  readJsonResponseWithByteLimit,
  ResponseBodyTooLargeError,
  responseByteLimitForApiRequest,
  shouldRetryRequest,
  TODO_STATE_TRANSPORT_RESPONSE_BYTES,
  withRetry
}
