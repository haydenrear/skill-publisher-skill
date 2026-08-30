"""skt — skill-lifecycle CLI for skill-manager homes.

The subcommands live in `skt.status`, `skt.check`, `skt.sync`, `skt.ticket`
and `skt.publish`. What is re-exported HERE is the part other code is meant
to import rather than shell out to: the typed surface over this home's
artifact graph, in the shape `git_issue_workflow` exports `wt` — so that
`from skt import stale, build` reads the way `from git_issue_workflow import
wt_new` does, and a caller never has to know which module a name lives in.
"""

from .artifacts import (
    Artifact,
    ArtifactError,
    ArtifactOutput,
    ArtifactsUnsupported,
    BuildRefused,
    BuildResult,
    BuildStep,
    CliUnavailable,
    HomeNotFound,
    ProbeTimeout,
    StaleReason,
    StaleSurvey,
    UnknownArtifact,
    build,
    list_artifacts,
    resolve_ids,
    stale,
)

__version__ = "0.8.1"

__all__ = [
    "Artifact",
    "ArtifactError",
    "ArtifactOutput",
    "ArtifactsUnsupported",
    "BuildRefused",
    "BuildResult",
    "BuildStep",
    "CliUnavailable",
    "HomeNotFound",
    "ProbeTimeout",
    "StaleReason",
    "StaleSurvey",
    "UnknownArtifact",
    "build",
    "list_artifacts",
    "resolve_ids",
    "stale",
    "__version__",
]
