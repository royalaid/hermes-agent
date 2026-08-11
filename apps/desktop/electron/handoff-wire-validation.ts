import fs from 'node:fs'
import path from 'node:path'

const HANDOFF_CAPABILITY_PATTERN = /^[A-Za-z0-9._-]{16,128}$/

export type Realpath = (file: string) => string

export function hasExactKeys(value: unknown, expected: readonly string[]): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()

  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index])
}

export function hasHandoffCapabilitySyntax(value: string): boolean {
  return HANDOFF_CAPABILITY_PATTERN.test(value)
}

export function resolveCanonicalAbsolutePath(
  candidate: unknown,
  realpath: Realpath = fs.realpathSync.native
): string | null {
  if (typeof candidate !== 'string' || !path.isAbsolute(candidate)) {
    return null
  }

  try {
    return realpath(candidate)
  } catch {
    return null
  }
}

export function canonicalPathIdentity(candidate: string, platform: NodeJS.Platform = process.platform): string {
  return platform === 'win32' ? candidate.toLowerCase() : candidate
}

export function sameCanonicalPath(left: string, right: string, platform: NodeJS.Platform = process.platform): boolean {
  return canonicalPathIdentity(left, platform) === canonicalPathIdentity(right, platform)
}
