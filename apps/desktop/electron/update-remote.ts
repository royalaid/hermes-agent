/**
 * Pure helpers for choosing a remote URL during passive update checks.
 *
 * A public install can end up with `origin=git@github.com:NousResearch/hermes-agent.git`.
 * If the user's GitHub SSH key is FIDO2/passkey-backed, a background `git fetch
 * origin` triggers an unexplained hardware-touch prompt. For passive checks
 * against the official repo we substitute the public HTTPS `ls-remote` path,
 * which needs no auth and cannot prompt. Active update/apply flows are left
 * unchanged.
 *
 * Extracted from main.ts so the security-critical remote detection is unit
 * testable without booting Electron (main.ts requires('electron') at load).
 */

const OFFICIAL_REPO_HTTPS_URL = 'https://github.com/NousResearch/hermes-agent.git'
const OFFICIAL_REPO_CANONICAL = 'github.com/nousresearch/hermes-agent'

// Normalize common GitHub remote URL forms to `host/owner/repo` (lowercased,
// no trailing slash, no .git suffix) so SSH and HTTPS forms of the same repo
// compare equal.
function canonicalGitHubRemote(url) {
  if (!url) {
    return ''
  }

  let value = String(url).trim()

  if (value.startsWith('git@github.com:')) {
    value = `github.com/${value.slice('git@github.com:'.length)}`
  } else if (value.startsWith('ssh://git@github.com/')) {
    value = `github.com/${value.slice('ssh://git@github.com/'.length)}`
  } else {
    try {
      const parsed = new URL(value)

      if (parsed.hostname && parsed.pathname) {
        value = `${parsed.hostname}${parsed.pathname}`
      }
    } catch {
      // Leave non-URL forms unchanged.
    }
  }

  value = value.trim().replace(/\/+$/, '')

  if (value.endsWith('.git')) {
    value = value.slice(0, -4)
  }

  return value.toLowerCase()
}

function isSshRemote(url) {
  const value = String(url || '')
    .trim()
    .toLowerCase()

  return value.startsWith('git@') || value.startsWith('ssh://')
}

function isOfficialSshRemote(url) {
  return isSshRemote(url) && canonicalGitHubRemote(url) === OFFICIAL_REPO_CANONICAL
}

/**
 * Pick the remote that owns the branch being updated.
 *
 * Fork checkouts may intentionally keep the official repository as `origin`
 * while tracking their integration branch from a second remote such as
 * `fork`. A configured branch remote is authoritative even when broken; the
 * official SSH URL remains the anonymous passive-check path.
 */
function resolveUpdateRemote({ branchRemote, branchRemoteUrl, originUrl }) {
  const configuredRemote = String(branchRemote || '').trim()
  const configuredUrl = String(branchRemoteUrl || '').trim()

  if (configuredRemote && configuredRemote !== '.') {
    if (isOfficialSshRemote(configuredUrl)) {
      return { name: OFFICIAL_REPO_HTTPS_URL, url: OFFICIAL_REPO_HTTPS_URL }
    }

    // The branch's configured remote remains authoritative even when its URL
    // cannot be read. Callers must probe/fetch that name and fail closed,
    // rather than silently checking origin and potentially applying another
    // repository's same-named branch.
    return { name: configuredRemote, url: configuredUrl }
  }

  if (isOfficialSshRemote(originUrl)) {
    return { name: OFFICIAL_REPO_HTTPS_URL, url: OFFICIAL_REPO_HTTPS_URL }
  }

  return { name: 'origin', url: String(originUrl || '').trim() }
}

export {
  canonicalGitHubRemote,
  isOfficialSshRemote,
  isSshRemote,
  OFFICIAL_REPO_CANONICAL,
  OFFICIAL_REPO_HTTPS_URL,
  resolveUpdateRemote
}
