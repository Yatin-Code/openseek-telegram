# Human-ready issue audit — 2026-11-10

The `ready-for-human` label does not mean all implementation needs a human. Of the 20 audited tickets, seven contain server/documentation work agents can execute, eight need a connected or released client environment, and five retain explicit human acceptance, account-login, or destructive teardown gates. These classifications do not waive native issue dependencies or campaign sign-offs.

Coding and independent review used OpenCode CLI **`opencode/crouton-alpha-free#max`**. The parent reviewed changes, ran live operations, rejected incomplete evidence and updated GitHub. The unavailable Luna workers were stopped without changes.

## Ticket routing

| Ticket | Agent work / remaining prerequisite |
|---|---|
| [#46](https://github.com/Yatin-Code/openseek-telegram/issues/46) | Agent can operate the install/QA workflow once a Tizen target is connected. |
| [#49](https://github.com/Yatin-Code/openseek-telegram/issues/49) | Provider browser currently requires sign-in; unfamiliar-person, terminal-free acceptance cannot be substituted by a token-CLI deployment. |
| [#50](https://github.com/Yatin-Code/openseek-telegram/issues/50) | Agent wizard work already passed; comparison needs the missing #49 deployment. |
| [#51](https://github.com/Yatin-Code/openseek-telegram/issues/51) | Backup/restore/upgrade work already passed; destructive campaign teardown needs agreed timing and authorization. |
| [#52](https://github.com/Yatin-Code/openseek-telegram/issues/52) | Client/emulator prerequisite; agent execution possible once available. |
| [#53](https://github.com/Yatin-Code/openseek-telegram/issues/53) | Live dashboard pair/device/revoke/generate/scan/rebuild verified. Sharing/guest controls and complete screenshots remain. |
| [#54](https://github.com/Yatin-Code/openseek-telegram/issues/54) | Released client and actual frame/display comparisons required. |
| [#56](https://github.com/Yatin-Code/openseek-telegram/issues/56) | Agent criteria already checked; remaining maintainer acceptance is human. |
| [#58](https://github.com/Yatin-Code/openseek-telegram/issues/58) | Schema double apply passed. Boundary/expiry automation needs contribution authorization; global guest contributions are OFF. Shared table-drop remains a separate destructive step. |
| [#59](https://github.com/Yatin-Code/openseek-telegram/issues/59) | All technical criteria checked after live env/redeploy/rollback. Maintainer sign-off remains. |
| [#60](https://github.com/Yatin-Code/openseek-telegram/issues/60) | Requires the installed/released client environment; agent can execute many checks. |
| [#61](https://github.com/Yatin-Code/openseek-telegram/issues/61) | Needs released Tizen screens in ES/FR/JA and visual acceptance. |
| [#62](https://github.com/Yatin-Code/openseek-telegram/issues/62) | Device/client prerequisite; not inherently human-only implementation. |
| [#63](https://github.com/Yatin-Code/openseek-telegram/issues/63) | Public unreachable-origin and TLS-pin refusal checked. Provider backoff, live kill/restart and full write-key drill remain; partial exact reads need a protocol decision. |
| [#64](https://github.com/Yatin-Code/openseek-telegram/issues/64) | Requires a usable client/device for its real interaction checks. |
| [#65](https://github.com/Yatin-Code/openseek-telegram/issues/65) | Agent documentation corrections and Linux local-only container run completed. README-verbatim/all-command/all-screen/provider acceptance remains unproven. |
| [#66](https://github.com/Yatin-Code/openseek-telegram/issues/66) | Three deployed security criteria checked; OPTIONS mismatch #81, runtime-log coverage and complete exposure acceptance remain. |
| [#67](https://github.com/Yatin-Code/openseek-telegram/issues/67) | An unfamiliar person and two beta users are explicitly required. |
| [#74](https://github.com/Yatin-Code/openseek-telegram/issues/74) | Destructive shared-asset teardown and campaign ordering require a human decision. |
| [#76](https://github.com/Yatin-Code/openseek-telegram/issues/76) | Source translation/alignment complete at client af831c8f; published v2.0.8 still predates it. Release and on-device screen verification remain. |

SDB 4.2.36 is installed, but no device is connected and no Tizen emulator image is installed. Client source checks do not substitute for the missing screen evidence. The #51/#74 teardown ordering cycle is not silently waived.

## Verified changes

- Fixed [#80](https://github.com/Yatin-Code/openseek-telegram/issues/80): optional blank IMDb caused a late `bad_hints` failure. Empty/whitespace now behaves like omitted input; explicit invalid nonempty values retain refusal. Local commit `7f1dee0`.
- Added a public HTTP/transport regression for interrupted channel publication and restart/retry, preserving sparse exact reads and candidate invisibility until the hints manifest. Local commit `bae99f8`.
- Corrected prerequisites, the wizard-link typo, librarian quota scope and moderation commands, and source-vs-release localization evidence.
- Full backend suite: **551 passed in 66.71 seconds**; focused generation/channel checks: **62 passed**. No configured typechecker was added.
- Real dashboard generation completed, then NVMT and manifest read-back returned 200 after Scan/Rebuild. NVMT: 20,464 bytes, SHA256 `2cb95f8c71387be679860c2aec64fcd5ed7b36175e3ae8bac5f27413819ded1f`. Both temporary devices were revoked and the local node stopped.
- Persistent Supabase schema applied twice: counts stayed 29 tiles / 21 hints / 0 episodes / 12 tables; view invoker security and role grants unchanged.
- Vercel env/redeploy/rollback used one stable production alias and verified its actual deployment target before/new/after. Cache 2 GiB → 3 GiB → 2 GiB; temporary env ID removed; protected state unchanged. Build/deployment logs scanned; runtime logs are not claimed.
- Security matrix: 21/22 expected responses. Source-address, path and auth checks passed. Unsupported `OPTIONS /v1/health` returned 404 instead of documented 405: [#81](https://github.com/Yatin-Code/openseek-telegram/issues/81), ready for an agent. The full security ticket remains open.

H8 uses the saved **measurement-only** latency policy: the earlier verified 600-second idle and first GET 200 in 3794.078 ms satisfy recording without an invented numeric threshold. The ticket wording now reflects that recorded policy.

Thirteen existing acceptance checkboxes were checked across #58/#59/#63/#66/#80, plus six explicit completed sub-checks in #53/#76. Aggregate and unexecuted criteria remain open. No `qa:user-pass` or campaign closure was fabricated. Code commits are local and have not been pushed.

## Standards

Independent review reported a PR-scope concern and an optional duplicated-test-setup smell. There is no PR; implementation, regression and documentation are separate commits. Existing test setup style was retained. No outstanding documented code-standard violation was identified.

## Spec

Independent review found two documentation inaccuracies: quota scope was too broad and the version command appeared to verify venv support. Both were corrected and re-read against the source. The reviewer confirmed the blank-IMDb fix and interrupted-publication compatibility behavior match their scoped specs.

Review outcome: Standards — one scope concern addressed by separate commits, one optional style observation; Spec — two findings fixed.

## Evidence

Durable private run directory: `~/.local/state/openseek/qa-agent-campaign/implement-20261110/`.

- `supervision.json`, OpenCode JSON event logs and task prompts record delegation and parent corrections.
- `reviews/` holds independent standards/spec reports and their disposition.
- `dashboard/` holds screenshots, request-line summaries, read-back and negative-drill reports.
- `schema/` holds exact SQL and committed before/after results.
- `cloud/reports/` holds alias-verified rollback, logs, access prerequisites and the deployed security matrix.
- `docs-client/clean-container-transcript*.log` retains both the initial missing-container-declaration refusal and the successful scoped rerun.
- `ticket-updates/results.json` records every evidence comment and acceptance update.

Evidence comments:

- [#53](https://github.com/Yatin-Code/openseek-telegram/issues/53#issuecomment-6749711153)
- [#58](https://github.com/Yatin-Code/openseek-telegram/issues/58#issuecomment-6749712754)
- [#59](https://github.com/Yatin-Code/openseek-telegram/issues/59#issuecomment-6749714877)
- [#63](https://github.com/Yatin-Code/openseek-telegram/issues/63#issuecomment-6749718076)
- [#65](https://github.com/Yatin-Code/openseek-telegram/issues/65#issuecomment-6749719815)
- [#66](https://github.com/Yatin-Code/openseek-telegram/issues/66#issuecomment-6749721911)
- [#76](https://github.com/Yatin-Code/openseek-telegram/issues/76#issuecomment-6749723593)
- [#80](https://github.com/Yatin-Code/openseek-telegram/issues/80#issuecomment-6749725241)
