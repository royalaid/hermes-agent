/**
 * Choose the update target when the user has not explicitly pinned one.
 *
 * Source installs normally update their checked-out branch, but only when that
 * branch is actually published by the selected remote. Detached HEADs and
 * local-only work remain conservative and use the normal `main` target.
 */
export function resolveDefaultUpdateBranch({
  configuredBranch,
  currentBranch,
  published
}: {
  configuredBranch?: string
  currentBranch?: string
  published: boolean
}): string {
  const configured = configuredBranch?.trim()

  if (configured) {
    return configured
  }

  const current = currentBranch?.trim()

  return published && current && current !== 'HEAD' ? current : 'main'
}
