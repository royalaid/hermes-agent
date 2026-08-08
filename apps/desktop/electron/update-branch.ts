/**
 * Choose the update target when the user has not explicitly pinned one.
 *
 * Source installs normally update their checked-out branch. Only a definitive
 * missing-ref result retargets them to `main`; a transient probe failure keeps
 * the current branch so the later fetch can surface the real error. Detached
 * HEADs and local-only work remain conservative and use the normal target.
 */
export type PublicationState = 'present' | 'missing' | 'unknown'

export function branchPublicationProbeArgs(remote: string, branch: string): string[] {
  return ['ls-remote', '--exit-code', '--heads', remote, `refs/heads/${branch}`]
}

export function publicationStateFromExitCode(code: number | null | undefined): PublicationState {
  if (code === 0) {
    return 'present'
  }

  return code === 2 ? 'missing' : 'unknown'
}

export function resolveDefaultUpdateBranch({
  configuredBranch,
  currentBranch,
  publication
}: {
  configuredBranch?: string
  currentBranch?: string
  publication: PublicationState
}): string {
  const configured = configuredBranch?.trim()

  if (configured) {
    return configured
  }

  const current = currentBranch?.trim()

  if (!current || current === 'HEAD') {
    return 'main'
  }

  return publication === 'missing' ? 'main' : current
}
