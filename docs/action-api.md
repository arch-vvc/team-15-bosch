# Integrating the organizer Action API

The agent must change the demo only through the official Action API, after explicit human approval. The public OpenTelemetry repository does not establish that hackathon-specific contract. No endpoints have been guessed or tested against a real Action API.

Copy `config/action-contract.example.json` to `config/action-contract.local.json` only when you have the actual documentation. The example is intentionally invalid/unconfirmed. Populate:

- `base_url`: the local Action API HTTP address.
- `reviewed_by`: the operator who checked the mapping against organizer documentation.
- `operator_confirmed`: true only after that review.
- `actions`: each allowed action's ID, service, description, documented POST path, and fixed JSON body.
- `accepted_field` and `accepted_value`: the documented top-level JSON response field/value proving that the request was accepted. Acceptance is not recovery.
- Optional `context_fields`: maps internal keys `incident_id`, `proposal_id`, `approval_digest`, `approver` to documented top-level request fields. Context cannot overwrite fixed action parameters.
- Optional `header_env`: maps `Authorization` or `X-API-Key` to the name of a local environment variable. Put credentials in the environment, never in the contract or fixed body.

The production fallback model receives telemetry and no remedy actions. Its action_id must be null. Code selects the remedy from a validated signature; the model cannot supply endpoint paths or parameters. The complete endpoint mapping is fingerprinted into the approval-bound proposal. Changing it invalidates an old action selection.

Only loopback HTTP is supported. Redirects are refused. Mutation requests have a fifteen-second timeout, are made once, and have a bounded response size. A timeout or unexpected response leaves the outcome uncertain and escalates rather than repeating the request.

This adapter supports fixed JSON POST operations and a top-level acceptance predicate. If the official API uses other methods, nested acceptance fields, a separate approval-token protocol, idempotency/status endpoints, or different authentication, extend and test the adapter from that contract before enabling it. The local approval record does not replace any approval token required by the organizer API.

Use the same contract when investigating and executing. Dry-run first. Testing the adapter against a temporary local synthetic HTTP server is not proof of interoperability with the official API.

No rollback is executed implicitly. If the official runbook provides a reversible recovery action, present it as its own exact proposal and obtain approval. Until that mapping is tested, failed recovery is escalated with evidence.


## Rules-first mapping fields

Each action used by the current pipeline needs `tier` (1 or 2), `signatures` (a list of validated rule identifiers), and optionally `rollback_action_id`. A missing tier or unmatched signature cannot produce a live proposal. Built-in signatures are `container_stopped`, `oom_killed`, and `restart_loop`; `config/rules.json` defines resource signatures. Only one action may match a signature/target at a time, otherwise the incident escalates.

Rollback mappings must target the same service and describe an actual documented inverse. Rollback uses strict tier 2 approval regardless of the inverse action's normal tier, and consumes one of the two total mutation slots. Never supply a guessed rollback or a guessed API endpoint.
