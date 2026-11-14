# Agent model-output admission

Model output is untrusted input. A syntactically valid Python dictionary can
still contain unknown fields, non-finite confidence values, oversized text, an
unsupported priority, or a malformed retrieval query. Persisting that output—or
using it to request a tool—turns an implicit prompt convention into an execution
boundary.

`service_desk.agent_contracts` defines a strict contract for each supported
capability:

| Capability | Required output |
|---|---|
| `triage` | category token, supported priority, confidence, unique reason codes |
| `summarize` | bounded single-line summary and confidence |
| `recommend_action` | recommendation token, confidence, human-review boolean |
| `retrieve_knowledge` | bounded non-empty query and confidence |

Every contract rejects missing and unknown fields. Confidence must be finite and
inside `[0, 1]`; booleans are not accepted as numbers. Tokens are lowercase and
bounded, text excludes control characters, and list cardinality is explicit.
Validation runs immediately after the model call and before a tool-call record
is created.

## Failure handling

The worker persists one of four stable codes instead of raw exception text:

- `agent_output_invalid`
- `tool_policy_violation`
- `dependency_timeout`
- `agent_execution_failed`

This prevents provider messages, SQL details, tool arguments, credentials, or
customer content embedded in exceptions from entering the durable job record.
Detailed debugging belongs in a separately governed telemetry path with
redaction, access control, sampling, and retention—not in the workflow state.

## Boundary and limitations

The validator proves structural conformance, not factual accuracy, policy
compliance, calibration, or safety. It does not authenticate the model provider
or prevent a conforming query from retrieving inappropriate content. Tool
admission, human approval, evidence lineage, and downstream response validation
remain independent controls.

The current contracts are intentionally flat and capability-specific. Schema
changes should be versioned rather than relaxed in place. The next increment
should bind the accepted contract version and normalized-output digest into the
agent-job audit event without retaining private model content.
