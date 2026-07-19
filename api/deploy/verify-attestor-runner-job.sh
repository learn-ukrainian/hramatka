#!/bin/sh
# Root-owned, pre-job admission guard for the dedicated attestation runner.
#
# GitHub invokes this through ACTIONS_RUNNER_HOOK_JOB_STARTED after assignment
# but before workflow actions or shell steps. Keep it outside the runner's
# writable installation tree and do not print event content or credentials.
set -eu

readonly expected_repository='learn-ukrainian/learn-ukrainian-infra-private'
readonly expected_workflow_ref='learn-ukrainian/learn-ukrainian-infra-private/.github/workflows/review-attestation.yml@refs/heads/main'
readonly runner_node='/opt/actions-runner-attestor/externals/node24/bin/node'

reject() {
    printf '%s\n' 'attestor runner guard: rejected job context' >&2
    exit 1
}

[ "${GITHUB_ACTIONS:-}" = 'true' ] || reject
[ "${GITHUB_REPOSITORY:-}" = "$expected_repository" ] || reject
[ "${GITHUB_WORKFLOW_REF:-}" = "$expected_workflow_ref" ] || reject
[ "${GITHUB_EVENT_NAME:-}" = 'pull_request_target' ] || reject

case ${GITHUB_JOB:-} in
    attest|publish) ;;
    *) reject ;;
esac

event_path=${GITHUB_EVENT_PATH:-}
[ -n "$event_path" ] && [ -f "$event_path" ] && [ ! -L "$event_path" ] || reject

# Parse the runner-provided event without echoing it. The event payload is
# untrusted data, and only the labelled attestation trigger is admissible. Use
# the immutable runner-bundled Node binary, not a host interpreter or PATH.
[ -x "$runner_node" ] || reject
"$runner_node" - "$event_path" >/dev/null 2>&1 <<'NODE' || reject
const fs = require("node:fs");

try {
    const eventPath = process.argv[2];
    if (typeof eventPath !== "string" || process.argv.length !== 3) {
        throw new Error("missing event path");
    }
    const eventStat = fs.lstatSync(eventPath);
    if (!eventStat.isFile() || eventStat.size > 1_000_000) {
        throw new Error("invalid event file");
    }
    const event = JSON.parse(fs.readFileSync(eventPath, "utf8"));
    if (event === null || Array.isArray(event) || typeof event !== "object") {
        throw new Error("invalid event payload");
    }
    const label = event.label;
    if (
        event.action !== "labeled" ||
        label === null ||
        Array.isArray(label) ||
        typeof label !== "object" ||
        label.name !== "review-attestation"
    ) {
        throw new Error("unexpected event");
    }
} catch {
    process.exit(1);
}
NODE
