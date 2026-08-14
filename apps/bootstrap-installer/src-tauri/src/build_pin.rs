pub const DEFAULT_REPOSITORY: &str = "NousResearch/hermes-agent";

pub fn select_repository_pin(
    explicit: Option<&str>,
    github_repository: Option<&str>,
    upstream_remote_url: Option<&str>,
    origin_remote_url: Option<&str>,
) -> Result<String, String> {
    let repository = explicit
        .filter(|value| !value.trim().is_empty())
        .map(str::trim)
        .or_else(|| {
            github_repository
                .filter(|value| !value.trim().is_empty())
                .map(str::trim)
        })
        .or_else(|| upstream_remote_url.and_then(repository_from_remote_url))
        .or_else(|| origin_remote_url.and_then(repository_from_remote_url))
        .unwrap_or(DEFAULT_REPOSITORY);

    validate_repository(repository)?;
    Ok(repository.to_string())
}

pub fn repository_from_remote_url(remote: &str) -> Option<&str> {
    let remote = remote.trim().strip_suffix(".git").unwrap_or(remote.trim());
    let repository = remote
        .strip_prefix("https://github.com/")
        .or_else(|| remote.strip_prefix("http://github.com/"))
        .or_else(|| remote.strip_prefix("ssh://git@github.com/"))
        .or_else(|| remote.strip_prefix("git@github.com:"))?;

    validate_repository(repository).ok()?;
    Some(repository)
}

pub fn validate_repository(repository: &str) -> Result<(), String> {
    let mut parts = repository.split('/');
    let valid_part = |part: &str| {
        !part.is_empty()
            && part != "."
            && part != ".."
            && part
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.'))
    };

    if !parts.next().is_some_and(valid_part)
        || !parts.next().is_some_and(valid_part)
        || parts.next().is_some()
    {
        return Err(format!(
            "repository {repository:?} must be a GitHub owner/repository name"
        ));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repository_pin_prefers_explicit_then_ci_then_tracking_remote() {
        assert_eq!(
            select_repository_pin(
                Some("explicit/hermes-agent"),
                Some("ci/hermes-agent"),
                Some("git@github.com:fork/hermes-agent.git"),
                Some("https://github.com/NousResearch/hermes-agent.git"),
            )
            .unwrap(),
            "explicit/hermes-agent"
        );
        assert_eq!(
            select_repository_pin(
                None,
                Some("ci/hermes-agent"),
                Some("git@github.com:fork/hermes-agent.git"),
                Some("https://github.com/NousResearch/hermes-agent.git"),
            )
            .unwrap(),
            "ci/hermes-agent"
        );
        assert_eq!(
            select_repository_pin(
                None,
                None,
                Some("git@github.com:fork/hermes-agent.git"),
                Some("https://github.com/NousResearch/hermes-agent.git"),
            )
            .unwrap(),
            "fork/hermes-agent"
        );
    }

    #[test]
    fn repository_pin_falls_back_to_origin_then_canonical() {
        assert_eq!(
            select_repository_pin(
                None,
                None,
                None,
                Some("https://github.com/local/hermes-agent.git"),
            )
            .unwrap(),
            "local/hermes-agent"
        );
        assert_eq!(
            select_repository_pin(None, None, None, Some("https://example.com/local/repo.git"))
                .unwrap(),
            DEFAULT_REPOSITORY
        );
    }

    #[test]
    fn repository_pin_rejects_invalid_overrides_and_lookalike_hosts() {
        assert!(select_repository_pin(Some("../escape"), None, None, None).is_err());
        assert_eq!(
            repository_from_remote_url("https://github.com/owner/repo/extra.git"),
            None
        );
        assert_eq!(
            repository_from_remote_url("https://evil.example/github.com/owner/repo.git"),
            None
        );
    }
}