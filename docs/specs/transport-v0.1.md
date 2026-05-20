# TraceMind Transport Protocol v0.1

**Version**: tracemind.io/transport/v0.1
**Status**: Specification (Phase 6 Stage 6-2)
**Scope**: Wire protocol for cross-node agent communication. Defines the HTTP and FileQueue transport implementations introduced in `tm/transport/`, the envelope shape shared by both, and the retry / timeout / auth semantics consumed by the remote `AgentRuntime` resolver.

---

## 1. Scope & Non-Goals

### In scope (v0.1)

- Shared JSON envelope for all transport kinds
- HTTP/1.1 mapping (`HttpTransport`) — inbox (fire-and-forget) + RPC (request/response)
- FileQueue mapping (`FileQueueTransport`) — atomic filesystem envelopes with at-least-once + idempotency dedup
- Retry / timeout / auth semantics for remote `AgentRuntime` wiring
- Failure-injection harness (`FailureInjectingTransport`) for deterministic fault testing

### Out of scope (v0.1)

- FastAPI route registration (Stage 6-3 mounts `/_transport/inbox` and `/_transport/rpc` on tm-server)
- gRPC / WebSocket / mesh transports
- Cross-language SDKs (the wire format is JSON-only so any HTTP client can interoperate; SDKs are Phase 8+)

### Principle

Transports are **replaceable implementations** of the `Transport` Protocol (`tm/transport/base.py`). Topology (star / tree) is declared by `AgentNetwork` artifacts (K-Ontology v0.3), not hard-coded in transport code.

---

## 2. Shared envelope

Every message — regardless of transport kind — uses this JSON object:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `from` | string | yes | Sender peer_id (audit provenance) |
| `to` | string | yes | Recipient peer_id |
| `kind` | enum: `send` \| `request` | yes | Fire-and-forget vs synchronous RPC |
| `body` | object | yes | Opaque payload; remote agent RPC uses `{op, agent_id, inputs, config}` |
| `idempotency_key` | string | no | Receiver-side dedup; **required for retried RPC** |
| `correlation_id` | string | no | FileQueue RPC only; links request → response file |
| `seq` | integer | no | FileQueue monotonic sequence (filename ordering) |
| `uuid` | string | no | FileQueue unique file suffix |
| `timestamp` | string (RFC3339) | no | FileQueue audit timestamp |

Remote agent invocation envelope (inside `body`):

```json
{
  "op": "agent.run",
  "agent_id": "leaf.observer",
  "inputs": {"metric": 42},
  "config": {}
}
```

Successful RPC reply:

```json
{"outputs": {"echo": {"metric": 42}}}
```

Error reply (proxy raises `RemoteAgentEscalation`):

```json
{"error": "handler exploded"}
```

---

## 3. HTTP transport

### Endpoints

| Method | Path | Purpose | Success |
|--------|------|---------|---------|
| POST | `{base_url}/_transport/inbox` | Fire-and-forget delivery | HTTP 202 |
| POST | `{base_url}/_transport/rpc` | Synchronous RPC | HTTP 200 + JSON reply |

`HttpTransport` appends these paths to each peer's base URL registered in `peer_endpoints`.

### Auth

When `AgentRuntime.auth_ref` is set, the resolver looks up the token via `TransportBuildContext.auth_tokens` or an `auth_resolver` callback and sets the `Authorization` header on every outgoing request. Tokens MUST NOT be inlined in AgentBundle YAML — use opaque secret-store references.

### Retry policy

Default: 3 attempts, exponential backoff (0.1s base, 2.0s cap).

| Condition | Retried? |
|-----------|----------|
| `httpx.NetworkError` (connect/read failure) | yes |
| `httpx.TimeoutException` | yes |
| HTTP 5xx | yes |
| HTTP 4xx | **no** (deterministic client error) |

Configurable via `AgentRuntime.retry_policy` → `RetryPolicySpec` → live `RetryPolicy`.

### Idempotency

Receiver maintains `(from, idempotency_key)` dedup state. Duplicate inbox deliveries are silently absorbed. Duplicate RPC replays the cached reply.

Server-side hooks (framework-agnostic):

- `HttpTransport.push_inbox(envelope)` — deposit fire-and-forget message
- `HttpTransport.handle_rpc(envelope)` — dispatch to registered handler
- `make_inbox_handler(transport)` / `make_rpc_handler(transport)` — FastAPI-ready callables (Stage 6-3)

---

## 4. FileQueue transport

### Directory layout

```
{root}/
  peers/
    {peer_id}/
      inbox/        # pending messages (FIFO by filename seq prefix)
      processed/    # successfully consumed (audit trail)
      responses/    # RPC replies keyed by correlation_id
      dedup.json    # rolling LRU of seen idempotency keys
```

### Semantics

- **At-least-once**: atomic write via `tempfile` + `os.replace`; crash between write and processed-rename leaves message in inbox for re-delivery
- **FIFO**: filename `{seq:020d}-{uuid}.json`; `sorted(inbox)` yields oldest-first
- **Idempotency**: rolling window (default 256 keys) persisted to `dedup.json`
- **RPC**: `request()` writes to recipient inbox, polls `{my_peer_id}/responses/{correlation_id}.json`

Server-side drain:

- `FileQueueTransport.process_request_inbox(max_messages=N)` — invoke registered handlers, write replies

### Disconnected-leaf scenario

1. Center `send()` / `request()` writes envelope to leaf inbox (succeeds even if leaf process is down)
2. Leaf reconnects, calls `process_request_inbox()`
3. Leaf writes reply to center's `responses/` dir
4. Center's pending `request()` poll picks up the reply

---

## 5. InProcess transport (default)

Synchronous in-memory FIFO mailboxes. Default for single-process bundles (Phase 5 behavior unchanged).

Stage 6-2 adds optional RPC via `register_request_handler(peer_id, fn)` — used by remote resolver unit tests and in-process simulation of cross-node agents without real I/O.

---

## 6. Remote AgentRuntime resolver

`tm/agents/remote_runtime.py` wires declarative `AgentRuntime` specs into live transports:

```
AgentRuntime (kind=remote, transport_kind=http|file_queue|inprocess)
    → build_transport_for_runtime(ctx)
    → RemoteRuntimeAgent (local proxy)
    → transport.request(peer_id, envelope)
```

Transport failures map to `RemoteAgentEscalation` with an escalation payload suitable for `EscalationReportBody` — never silently swallowed.

Registry integration (`tm/agents/registry.py`):

- `runtime.kind == "remote"` → `resolve_remote_agent()` (no factory registration needed)
- `runtime.kind == "inprocess"` → existing factory lookup (unchanged)

---

## 7. Failure injection (testing)

`FailureInjectingTransport` wraps any `Transport` and applies configurable faults:

| Kind | Effect |
|------|--------|
| `drop` | Swallow outgoing call (`send` silent; `request` raises `TransportNetworkError`) |
| `delay` | Sleep `seconds` before forwarding |
| `corrupt` | Mutate payload (default: prefix string values with `__corrupt__`) |
| `partition` | Refuse peers in `blocked_peers` set |
| `duplicate` | Forward twice (exercises idempotency dedup) |

Used by Stage 6-2 tests and Stage 6-3 cross-node escalation tests.

---

## 8. Error hierarchy

| Class | When |
|-------|------|
| `TransportError` | Base; protocol / validation failure |
| `TransportTimeout` | Operation exceeded deadline |
| `TransportNetworkError` | Peer unreachable; carries `peer_id` for escalation routing |
| `RemoteAgentEscalation` | Remote proxy failure; carries escalation dict |

---

## 9. Compatibility

- Phase 5 import path `tm.control.agents.transport` remains a re-export shim → `tm.transport.base`
- `InProcessTransport` API unchanged except additive `register_request_handler` / `request`
- Existing single-process bundles require zero configuration changes

---

## 10. References

- K-Ontology v0.3 AgentNetwork: [`k-ontology-v0.3.md`](k-ontology-v0.3.md)
- Implementation: `tm/transport/{base,http,file_queue,test_helpers}.py`
- Remote resolver: `tm/agents/remote_runtime.py`
- AgentRuntime schema: `tm/agents/schemas/agent_runtime.json`
- Phase 6 plan: Stage 6-2 task list + Stage 6-3 server route mounting
