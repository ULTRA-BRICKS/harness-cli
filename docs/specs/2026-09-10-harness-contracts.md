# Harness contracts: universal control and truthful inventory

Status: proposed implementation specification; no runtime implementation in this change.
Date: 2026-09-10.
Owners: harness-cli for adapters and discovery; consuming repositories for integration.

Epics: [control #22](https://github.com/alex-mextner/harness-cli/issues/22), [inventory #26](https://github.com/alex-mextner/harness-cli/issues/26).
Parent: [ecosystem #514](https://github.com/alex-mextner/rig-cli/issues/514).
Consumers: [Telegram #380](https://github.com/alex-mextner/tg-cli/issues/380), [review entities #487](https://github.com/alex-mextner/review-cli/issues/487), [review runtime #492](https://github.com/alex-mextner/review-cli/issues/492).

## Problem and verified baseline

The original request behind #22 is to keep ALL harness-specific behavior behind adapters, with a human/agent CLI and a suitable cross-platform programmatic API. A one-shot nudge is one useful delivery slice, not completion of that request.

At the inspected main tree `d238d13dd8859517800f5644c572859aa19f75d1`:

- `harness_cli/cli.py` exposes limits, search, tasks, agents and nudge. It does not expose inventory or messages.
- `limits_collect.collect_all` invokes Claude, Codex, z.ai, x.ai and Kimi collectors. That fixed list is not complete enumeration of accounts known to all harness stores.
- `agents --live` filters a status based on transcript activity. Recent mtime does not establish an addressable process or correct process generation.
- `omp_acp.nudge` spawns `omp acp`, initializes, loads the session, waits for the session/prompt RPC and terminates its child in finally. It does not provide a durable persistent permission lifecycle.
- [PR #23](https://github.com/alex-mextner/harness-cli/pull/23) delivered that narrower nudge seam. Its reported tests are prior evidence, not tests rerun during this grooming.
- The Telegram consumer still owns ACP types, method names and OMP discovery in `features/tg-ctl/omp.ts`; [tg-cli#377](https://github.com/alex-mextner/tg-cli/issues/377) tracks the remaining migration.

The same specification must distinguish observed, inferred, unknown and unsupported results. No transcript-derived token total may be presented as a provider quota percentage.

## Boundaries

Harness-cli owns provider/harness configuration discovery, protocol adapters, canonical session resolution and normalized factual observations. It must not copy Telegram transport, review board selection, ship-policy decisions or rig's installation/reconciliation engine.

Consumers call a supported API; they do not import individual provider internals or inspect OMP databases/session filenames independently. Existing public formats must be migrated deliberately, not silently reinterpreted.

## Inventory and health

### Entity model

An inventory snapshot has a schema version, observation time, source coverage and entities with stable opaque IDs. Keep the following concepts distinct: harness installation, harness kind, provider, vendor, account, model and model version, session, and capability. Review role/effort are consumer annotations, not alternative account identities.

A capability states supported/unsupported/unknown and a reason. Installation presence is not authentication; authentication is not paid entitlement; entitlement is not available quota; recent transcript activity is not process liveness.

Account identity must distinguish the credential source and route. Where two routes genuinely share an account, represent that relationship explicitly. Never merge accounts merely because their provider/model labels match, and never disclose tokens, authorization headers or private credential material.

### Coverage contract

`harness inventory --json` is a proposed new command owned by [#24](https://github.com/alex-mextner/harness-cli/issues/24). Offline discovery is the baseline; any live probing must be explicit and documented. An absent CLI, unreadable store, corrupt record, unconfigured source or unsupported API produces a coverage diagnostic, not an empty successful inventory.

[#21](https://github.com/alex-mextner/harness-cli/issues/21) owns complete account/limit coverage. Determine the expected set from all configured/discovered sources, then emit one normalized result or reason per expected account. Tests compare identity sets, not merely that a few cards exist. The static [rig inventory](https://github.com/alex-mextner/rig-cli/issues/501) is a historical baseline and refresh recipe, not proof of current coverage.

[#17](https://github.com/alex-mextner/harness-cli/issues/17) adds OMP-owned account and usage observations. Local usage history may establish observed usage, but cannot manufacture a provider quota denominator or percentage. A missing/corrupt OMP store must remain visible in coverage rather than disappear as an empty list.

### Health dimensions

Each observation carries source, observed_at, scope and confidence/availability semantics. Represent independently:

| Dimension | Required distinction |
| --- | --- |
| Authentication | confirmed authenticated, confirmed unauthenticated, unknown |
| Entitlement/billing | confirmed valid, confirmed blocked, unknown/unsupported |
| Quota | API-reported windows and reset times, exhausted evidence, unknown |
| Connectivity | reachable, transport failure, unknown |
| Local storage | readable, missing, corrupt/locked, unknown |
| Process/session | live generation verified, terminated, activity-only, unknown |

Do not map every failed limit card to unauthenticated. A network error or database lock says nothing definitive about credentials. Do not set authenticated=true for an unsupported provider. Freshness and incomplete collection travel with the data; consumers choose policy without rewriting observations.

[#18](https://github.com/alex-mextner/harness-cli/issues/18) provides `harness messages ID` using the SAME emitted agent ID. Reuse existing transcript parsers, default to the latest 20 messages in chronological order, support explicit n/all and JSON, sanitize terminal control sequences, and return a documented error for an unknown ID. This operation is read-only; sending and following are separate capabilities.

## Control and IPC

### Architecture decision still required

| Option | Good fit | Limitation |
| --- | --- | --- |
| Process per operation, JSON result | Inventory and bounded one-shot inspection; simple portability | Does not preserve permission streams or durable interactive sessions by itself |
| Persistent child with stdio NDJSON | Cross-language bidirectional requests/events without an OS socket dependency | Requires lifecycle, multiplexing, backpressure and reconnect semantics |
| Local socket service | Multiple independent clients and an explicit service owner | Unix sockets alone are not a Windows design; permissions, discovery and lifecycle add scope |

Recommended direction for review: preserve one-shot CLI reads, introduce a single persistent session owner with a transport-independent protocol, and use stdio NDJSON as the first cross-platform binding unless a demonstrated multi-client need requires another binding. This is a proposal, not a falsely recorded approval. Record the final choice and Windows behavior before implementation. Do not maintain two independent ACP engines.

### Proposed protocol requirements

Freeze exact names and schema during the API decision; the following are required semantics, not claims that these commands already exist:

- Version negotiation and a capability query.
- Session inspect/attach, accept-message, event subscription, permission decision and explicit cancellation/detach operations.
- An envelope with protocol version, request ID, operation, target identity and payload; responses distinguish accepted, completed, unsupported, not_found, conflict and transport/provider failures.
- Session identity includes native identity plus generation/lifecycle evidence. A PID, tmux pane ID, path or transcript mtime alone is insufficient.
- An operation ID persists across retries. `accepted` means the owning component can recover the intent after its documented crash boundary. `completed` is a separate event. A successful write to stdin alone is not proof of either provider delivery or turn completion.
- Events correlate to the operation and session generation. Consumers must not infer the recipient from a mutable active pane.
- Duplicate acceptance is idempotent. At-least-once delivery may require adapter support to prevent duplicate model work; exactly-once execution must not be advertised without that support. Unsupported guarantees are explicit capabilities.
- Pending permissions survive disconnect/reconnect. Reaping a failed transport must not erase the decision record. No new arbitrary expiry or wait-for-turn-completion mechanism is introduced to conceal delivery problems.

The existing one-shot `nudge` output/exit contract remains documented until a versioned migration replaces it. Preserve the intentional `tasks` plural cutover in [#19](https://github.com/alex-mextner/harness-cli/issues/19); do not silently reintroduce the removed singular alias.

### Migration

1. Specify envelopes, ownership and error semantics with hermetic fixtures.
2. Add the harness-owned persistent implementation and adapter conformance tests.
3. Migrate tg-cli#377 consumer call sites while preserving question/permission lifecycle behavior, including its existing retention contract.
4. Remove consumer protocol code only after end-to-end parity is demonstrated.
5. Validate a second adapter or fake implementation that requires no Telegram protocol changes. Adding configuration for an already supported capability is different from implementing a genuinely new protocol.

Inventory completeness and control migration share IDs/capabilities, but unrelated UI polish must not block a independently testable control increment.

## Test and closure matrix

| Area | Required evidence |
| --- | --- |
| Import/CLI | Help/version with stdlib-only imports; JSON stdout stays machine-readable |
| Inventory | Multiple accounts, absent binary, unreadable/corrupt stores, unsupported API and exact set coverage |
| Health | Unknown never healthy; network failure never automatically unauthenticated; no fabricated percent |
| Live identity | Recent dead transcript, stale pane, reused PID, restarted native session and simultaneous siblings |
| IPC | Interleaved requests/events, disconnect, duplicate operation ID, pending permission and child exit |
| Delivery | Persist-before-ack failure injection; accepted vs completed; unsupported idempotency surfaced |
| Consumer | Same generic fixture through CLI/IPC and tg-cli without ACP knowledge |
| CI | [#25](https://github.com/alex-mextner/harness-cli/issues/25): actual failing job reason and a successful run at the changed revision |

Use isolated HOME/tmp_path and mocked network as AGENTS.md requires. Do not probe credentials, send real prompts, rotate accounts or operate the user's live daemon to validate documentation. Closure requires source/PR revision, exact test commands with results, durable artifacts and separate live acceptance when requested. Documentation, a merged narrow PR and checked boxes do not substitute for the original outcome.
