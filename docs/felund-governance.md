# Felund Governance Protocol Sketch v1

*Designed to plug into the existing **encrypted event plane** (the “canonical encrypted envelope v2”) and the Plan v2 roadmap.*

This sketch assumes:

* Every event is carried inside your **encrypted message envelope** (`type`, headers, and `enc` payload).
* Every device has a stable `author_node_id` and a signing key (or at least the shared-circle secret + per-session keys).
* “Ejection” is only real if it triggers a **circle epoch bump + rekey**.

---

## 0) Threat model and scope

**Target use case:** small private circles (2–20 members), infrequent governance, mostly good-faith users.

**Security goals:**

* Local blocking works immediately (no coordination required).
* Circle actions (eject/promote/policy changes) are verifiable and deterministic.
* No single “anchor” has authority; anchors help propagate events only.
* A removed member cannot decrypt **new** messages after removal.

**Non-goals v1:**

* Secret ballots
* Complex delegation / liquid democracy
* Strong metadata privacy against circle members
* MLS-level group key agreement

---

## 1) Canonical envelope integration

All governance content is stored in the encrypted payload of the standard envelope. The *outer* envelope stays minimal:

```json
{
  "msg_id": "uuid-or-hash",
  "circle_id": "circle_hash",
  "channel_id": "governance", 
  "author_node_id": "node_abc",
  "created_ts": 1771730000,
  "type": "gov.propose",
  "enc": { "iv": "...", "ct": "...", "tag": "..." },
  "schema_version": 2
}
```

* `channel_id`: recommend a reserved governance channel like `"governance"` so all clients can display/audit it consistently.
* `type`: one of the event types below.
* The encrypted payload contains the event body (schemas below).

**Signing:**
If you already have per-device signing keys, sign the plaintext event body (or the whole envelope headers + ciphertext reference) and include the signature inside the encrypted payload. If you don’t yet have asymmetric keys, you can start with “authenticated via circle membership + AEAD” but it’s weaker for non-repudiation.

---

## 2) Terminology

* **Member**: a circle participant with the current epoch key.
* **Moderator**: a member with elevated ability to propose certain actions.
* **Owner** (optional): only used for “break glass” recovery. Not required in v1.
* **Epoch**: integer version of membership/keys. Rekey increments the epoch.

---

## 3) Core data model

### 3.1 Circle policy object

A deterministic policy object must be readable by all clients to evaluate votes. Store it as a replicated state, updated by `circle.policy.set`.

```json
{
  "policy_version": 1,
  "roles": {
    "owner": ["node_owner_1"],
    "moderators": ["node_mod_1", "node_mod_2"]
  },
  "governance": {
    "proposal_ttl_s": 86400,
    "min_proposal_interval_s": 600,
    "max_open_proposals": 3,

    "quorum": {
      "promote_mod": 2,
      "eject_member": 3,
      "policy_change": 3
    },

    "thresholds": {
      "promote_mod": { "mode": "majority_votes_cast" },
      "eject_member":  { "mode": "supermajority_votes_cast", "ratio_num": 2, "ratio_den": 3 },
      "policy_change": { "mode": "supermajority_votes_cast", "ratio_num": 2, "ratio_den": 3 }
    },

    "tie_break": "fail",
    "cooldowns": {
      "eject_failed_s": 86400
    }
  },

  "invites": {
    "who_can_invite": "moderators" 
  }
}
```

Notes:

* `tie_break: "fail"` means status quo wins ties (prevents power grabs).
* “votes cast” + quorum is usually the best trade-off in small groups. If you want stricter for tiny circles, add a policy rule: if `member_count <= 4`, require “majority_of_all_members” for ejection.

### 3.2 Membership snapshot

Clients must maintain a view of current membership and epoch:

```json
{
  "circle_epoch": 7,
  "members": ["node_a", "node_b", "node_c"],
  "roles": {
    "owner": ["node_a"],
    "moderators": ["node_b"]
  }
}
```

The membership changes only via `circle.member.add` / `circle.member.remove` and those events are only valid when accompanied by a completed **rekey**.

---

## 4) Event schemas

### 4.1 `gov.propose`

Creates a proposal. Proposals are evaluated against current circle policy.

```json
{
  "proposal_id": "uuid",
  "action": "EJECT_MEMBER | PROMOTE_MOD | DEMOTE_MOD | POLICY_CHANGE",
  "target_node_id": "node_x | null",
  "proposed_policy_patch": { /* only for POLICY_CHANGE */ },
  "reason": "optional short text",
  "created_ts": 1771730000,
  "expires_ts": 1771816400,
  "proposer_node_id": "node_abc",
  "policy_ref": { "policy_version": 1, "circle_epoch": 7 },

  "sig": { "alg": "ed25519", "value": "..." } 
}
```

**Validation rules (clients must enforce):**

* `expires_ts - created_ts <= policy.governance.proposal_ttl_s`
* proposer is a current member at `policy_ref.circle_epoch`
* proposer is allowed to propose this action:

  * `EJECT_MEMBER`: member or moderator? (recommend: **moderator or owner** by default; allow policy to change)
  * `PROMOTE_MOD/DEMOTE_MOD/POLICY_CHANGE`: moderator or owner (recommend)
* Must not exceed `max_open_proposals`
* Must respect `min_proposal_interval_s` per proposer
* If action is `EJECT_MEMBER`, enforce cooldown if there was a recent failed ejection for that target.

### 4.2 `gov.vote`

A member votes on a proposal.

```json
{
  "proposal_id": "uuid",
  "voter_node_id": "node_v",
  "choice": "YES | NO | ABSTAIN",
  "ts": 1771730500,

  "sig": { "alg": "ed25519", "value": "..." }
}
```

**Validation rules:**

* voter is a current member at the proposal’s `policy_ref.circle_epoch`
* vote occurs before proposal expiry
* one vote per voter per proposal (latest vote wins only if you allow updates; simplest is “first vote counts”)

### 4.3 `gov.resolve`

Resolution is deterministic and can be computed locally, but you may still emit a `gov.resolve` for UX and caching.

```json
{
  "proposal_id": "uuid",
  "resolved_ts": 1771734000,
  "result": "PASSED | FAILED | EXPIRED",
  "tally": {
    "yes": 3,
    "no": 1,
    "abstain": 0,
    "eligible_voters": 6,
    "votes_cast": 4
  },
  "policy_ref": { "policy_version": 1, "circle_epoch": 7 },
  "derived_by_node_id": "node_any",
  "sig": { "alg": "ed25519", "value": "..." }
}
```

**Rule:** clients must treat `gov.resolve` as a *hint*. The source of truth is the set of `gov.vote` events + policy.

### 4.4 `circle.role.set`

Applies a promotion/demotion after a passed proposal.

```json
{
  "target_node_id": "node_x",
  "role": "moderator",
  "op": "ADD | REMOVE",
  "based_on_proposal_id": "uuid",
  "policy_ref": { "policy_version": 1, "circle_epoch": 7 },
  "ts": 1771734100,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

### 4.5 `circle.policy.set`

Changes the policy after a passed proposal.

```json
{
  "policy_patch": { /* JSON patch style or full replace */ },
  "based_on_proposal_id": "uuid",
  "old_policy_version": 1,
  "new_policy_version": 2,
  "policy_ref": { "circle_epoch": 7 },
  "ts": 1771734200,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

### 4.6 Membership change + rekey events

Ejection must include epoch bump + new key distribution.

#### 4.6.1 `circle.rekey.begin`

```json
{
  "rekey_id": "uuid",
  "op": "REMOVE_MEMBER | ADD_MEMBER",
  "target_node_id": "node_x",
  "based_on_proposal_id": "uuid | null",
  "old_epoch": 7,
  "new_epoch": 8,
  "ts": 1771734300,
  "initiator_node_id": "node_anchor_or_mod",
  "sig": { "alg": "ed25519", "value": "..." }
}
```

#### 4.6.2 `circle.rekey.share`

Distributes the new epoch key to one recipient, encrypted pairwise.

```json
{
  "rekey_id": "uuid",
  "new_epoch": 8,
  "recipient_node_id": "node_r",
  "wrapped_epoch_key": {
    "alg": "x25519-hkdf-aesgcm | rsa-oaep | ...",
    "ct": "...",
    "iv": "...",
    "tag": "..."
  },
  "ts": 1771734350,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

> If you don’t have asymmetric keys yet: you can temporarily encrypt `wrapped_epoch_key` using an existing per-peer session key established over the direct connection. Long-term, device public keys make this cleaner.

#### 4.6.3 `circle.rekey.ack`

Recipient confirms they’ve installed the new epoch key.

```json
{
  "rekey_id": "uuid",
  "new_epoch": 8,
  "recipient_node_id": "node_r",
  "ts": 1771734400,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

#### 4.6.4 `circle.rekey.commit`

Finalizes membership change; from this point, the circle operates on `new_epoch`.

```json
{
  "rekey_id": "uuid",
  "old_epoch": 7,
  "new_epoch": 8,
  "members_after": ["node_a","node_b","node_c"],
  "removed_node_id": "node_x | null",
  "added_node_id": "node_y | null",
  "ts": 1771734500,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

#### 4.6.5 `circle.member.remove` / `circle.member.add`

Optionally emit explicit membership events, but many systems simply treat `rekey.commit` as the membership change.

---

## 5) Pass/fail rules (deterministic)

### 5.1 Proposal lifecycle

A proposal is in one of:

* `OPEN` (now <= expires_ts and not resolved)
* `EXPIRED` (now > expires_ts and not passed)
* `RESOLVED` (passed or failed once threshold reached or expired)

### 5.2 Counting eligible voters

Eligible voters are **members at the proposal’s referenced epoch** (`policy_ref.circle_epoch`).

If membership changes mid-vote (rare in small circles), rule is:

* proposals bind to the epoch at creation time; membership list is frozen for that vote.

### 5.3 Quorum

If `votes_cast < quorum[action]` at expiry → FAIL/EXPIRED.

### 5.4 Threshold modes

* `majority_votes_cast`: `yes > no` AND quorum met
* `supermajority_votes_cast`: `yes / votes_cast >= ratio_num/ratio_den` AND quorum met
* `majority_of_all_members` (optional): `yes >= floor(eligible_voters/2)+1`

### 5.5 Tie break

If `yes == no`, result is `FAIL` (status quo wins) unless policy says otherwise.

### 5.6 Early resolution (optional)

You can resolve early when:

* even if all remaining eligible voters voted YES, it can’t reach threshold → FAIL
* threshold is already met and cannot be overturned → PASS

This is a UX improvement; not required.

---

## 6) Local block protocol (non-governance)

Blocking should be **local first**, and optionally “reportable.”

### 6.1 Local-only block state (not broadcast)

Stored locally, not as circle events:

```json
{
  "blocked_nodes": {
    "node_x": { "since_ts": 1771730000, "reason": "optional local note" }
  }
}
```

**Effects (local policy):**

* Do not render `node_x` messages by default
* Do not accept direct calls from `node_x`
* Optionally do not relay/store their envelopes (recommended for anchors)

### 6.2 Optional report event (`gov.report`)

If a user wants to escalate or document a pattern:

```json
{
  "report_id": "uuid",
  "target_node_id": "node_x",
  "reporter_node_id": "node_r",
  "category": "SPAM | HARASSMENT | OTHER",
  "summary": "short description",
  "ts": 1771730600,
  "sig": { "alg": "ed25519", "value": "..." }
}
```

Reports are not votes. They’re context for human decision-making.

---

## 7) Epoch + rekey flow (the critical part)

### 7.1 Eject flow: end-to-end

**Goal:** remove member `X` so they cannot decrypt new messages.

1. Someone proposes ejection:

   * `gov.propose(action=EJECT_MEMBER, target=X)`
2. Members vote:

   * `gov.vote(...)`
3. Clients deterministically compute PASS.
4. A coordinator (often the current anchor or any moderator) starts rekey:

   * `circle.rekey.begin(op=REMOVE_MEMBER, target=X, old_epoch=E, new_epoch=E+1)`
5. Coordinator distributes new epoch key to each remaining member:

   * `circle.rekey.share(rekey_id, recipient=member_i, wrapped_epoch_key)`
6. Members ack:

   * `circle.rekey.ack(...)`
7. Once enough acks are received (policy can define “all” or “quorum”), commit:

   * `circle.rekey.commit(members_after=..., new_epoch=E+1)`
8. From commit onward:

   * All new messages must be encrypted under epoch `E+1`.
   * Nodes refuse to accept/emit new messages under old epoch except for late-arriving governance artifacts.

**Important behavior:**
Clients should keep old epoch keys only as long as needed to read history. If you want stronger “forward secrecy over history,” that’s a later feature.

### 7.2 What if the coordinator dies mid-rekey?

Because events are replicated, any eligible node can take over:

* If `rekey.begin` exists but no `commit` after a timeout, another node can re-issue missing `rekey.share` messages.
* Multiple coordinators are okay if `rekey_id` is the same and key material matches. To avoid mismatch:

  * The key material should be derived deterministically from:

    * `rekey_id` + a coordinator random seed published in `rekey.begin`, **or**
    * generated by coordinator and included (wrapped) identically to all.
* Simplest v1: coordinator generates a new random epoch key and wraps it per member; if a second coordinator tries, it must start a *new* `rekey_id` and supersede the old attempt (clients prefer the newest `rekey.begin` by timestamp).

### 7.3 Add-member flow (invite)

Same structure:

* Validate invite policy (who can invite)
* Begin rekey `ADD_MEMBER`
* Distribute new epoch key to all existing members + the new member
* Commit

---

## 8) Storage/indexing requirements (to plug into your gossip sync)

To support deterministic governance, every node must store:

* proposals keyed by `proposal_id`
* votes keyed by `(proposal_id, voter_node_id)`
* rekey sessions keyed by `rekey_id`:

  * begin
  * shares (per recipient)
  * acks
  * commit
* policy versions
* membership snapshots per epoch (or derivable from commits)

**Gossip integration:**
Your `MSGS_HAVE / MSGS_REQ / MSGS_SEND` anti-entropy exchange works unchanged. Governance events are just message types; prioritize them in sync for faster convergence.

---

## 9) Minimal “v1” rule set (recommended defaults)

If you want a concrete starting policy:

* **Promote moderator**

  * Propose: moderator or owner
  * Pass: majority votes cast, quorum 2, TTL 24h
* **Eject member**

  * Propose: moderator or owner (optionally any member with policy)
  * Pass: 2/3 votes cast, quorum 3, TTL 24h, cooldown 24h on failure
  * Rekey required, commit after acks from all remaining members (or quorum if you prefer availability)
* **Policy change**

  * Propose: owner or moderator
  * Pass: 2/3 votes cast, quorum 3, TTL 48–72h
* **Tie break:** fail

---

## 10) Implementation notes that save you pain later

* **Deterministic resolution** is the backbone. Treat “resolve” events as cache hints.
* Keep **proposal TTLs** long enough for time zones, short enough to prevent limbo.
* Keep **proposal rate limits** to prevent harassment via governance spam.
* Make **block local** and **eject deliberate**. That’s socially stabilizing.
* Make **membership change == epoch bump**. Otherwise “eject” is not real.

---

## 11) Where this maps onto Plan v2

This protocol aligns to:

* **Phase 1**: canonical encrypted envelope (required)
* **Phase 4**: call session control plane (shares the “event plane” pattern)
* A new governance slice can be slotted between Phase 1 and Phase 2 or after Phase 2:

  * Governance can run even on TCP-only as long as events are encrypted and replicated.


