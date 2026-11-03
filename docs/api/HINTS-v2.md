# Hints API v2 — NVMT shared pool

Status: frozen for implementation (ticket A1).
Applies to **NVMT packages only**. The preview/VTT API is a separate product surface and is never used by the
pool path. See `docs/api/CONTRACT-v1.md` for transport, auth, and the existing pool endpoints.

## Identity model

- `fp` is the exact blob key: SHA-256 over the client-observed size plus the first and last 64 KiB. A different
  release has a different `fp`, even when it is the same title.
- Hints are raw metadata, not an identity proof. They are never hashed into a new key and never replace `fp`.
- A hint match is always `approximate: true`. The client accepts the best candidate and may correct or reject it
  later; it must not decode or sample frames at request time to verify a candidate.

## Hint set

| Field | Type | Required | Validation |
|---|---|---|---|
| `imdb` | string | no* | `tt` + 1–10 digits, max 12 chars |
| `season` | int | no | 0–9999; `0` for a film |
| `episode` | int | no | 0–9999; `0` for a film |
| `duration_ms` | int | yes | 1–43,200,000 (12 h) |
| `size_bytes` | int | no | ≥ 0; client-claimed, used only as a tie-break |
| `filename` | string | no* | basename only, percent-encoded, ≤ 200 bytes before normalization |

\* At least one of `imdb` or `filename` is required on the candidate query. `duration_ms` alone is not enough:
it would match unrelated titles.

The server normalizes `filename` before storing it: basename, lowercase, extension removed, runs of
non-alphanumeric characters collapsed to `.`, leading/trailing dots removed, truncated to 200 chars. It never
stores a path, URL, header, token, or raw release-group tag beyond that normalized name.

## Candidate query

```
GET /v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms=7234000
    &size_bytes=1234567890&filename=movie.2024.1080p&exclude_fp=<64 hex>
```

- All hint fields are optional except `duration_ms`; at least one of `imdb` or `filename` is required.
- `exclude_fp` omits the caller's own package.
- Auth follows the existing pool read rules (paired device token or read key).
- Response is `200 {"candidates":[...]}` with at most 8 entries, best first. `Cache-Control: no-store`.

```json
{
  "candidates": [
    {
      "fp": "<64 lowercase hex>",
      "profile": "nvmt-v1-g5",
      "duration_ms": 7230000,
      "n_slots": 1446,
      "n_tiles": 724,
      "score": 100,
      "match": "imdb+season+episode+duration",
      "relation": "same_length",
      "approximate": true,
      "a_num": 1,
      "a_den": 1,
      "b_ms": 0,
      "source_duration_ms": 7230000
    }
  ]
}
```

`score` is an integer 0–100. `match` is a `+`-joined set drawn from `imdb`, `season`, `episode`, `duration`,
`filename`, `size`. `relation` is one of:

| `relation` | Meaning |
|---|---|
| `same_length` | durations differ by ≤ 1000 ms |
| `speed_24_25` | 24/25 or 25/24 speed ratio within 0.15% |
| `length_differs` | within the hard cutoff but not one of the above |

Hard cutoff: a candidate whose duration differs by more than `max(90_000, 15% of duration_ms)` is never returned.

## Ranking

Ordered by:

1. `score` descending.
2. `size_bytes` within 1% of the request (closer first) when both sides have a size.
3. Absolute duration difference ascending.
4. Newest `created` first.

Score tiers:

| Score | Match |
|---|---|
| 100 | `imdb` + `season` + `episode` exact, `same_length` |
| 95 | `imdb` + `season` + `episode` exact, `speed_24_25` |
| 90 | `imdb` + `season` + `episode` exact, `length_differs` |
| 85 | `imdb` exact only (film; season/episode 0) |
| 75 | `filename` exact, `same_length` |
| 65 | `filename` token overlap ≥ 0.6, `same_length` |
| — | anything below 65 is not returned |

The client accepts `candidates[0]` at `score >= 60` and `relation != length_differs`; below that it falls back
to local generation. The server does not hide low-confidence candidates; it labels them.

## Timing mapping

```
t_target_ms = (t_source_ms * a_num) / a_den + b_ms
```

- `a_num / a_den` is the reduced integer ratio `target_duration / source_duration`.
- `b_ms` is a known offset; `0` unless a per-device offset or a verified mapping exists.
- The client clamps to `[0, target_duration_ms]` and drops out-of-range tiles.
- Same-length candidates use `a_num = a_den = 1`, `b_ms = 0`.

## Upload

```
PUT /v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=7230000
    &imdb=tt1234567&season=0&episode=0&size_bytes=1234567890&filename=movie.2024.1080p
Content-Type: application/vnd.openseek.nvmt
```

- The hint query is the same set as the candidate query, minus `exclude_fp`.
- `duration_ms` is required; it is the package duration and must pass the existing NVMT validation.
- `episode_key` remains accepted for legacy clients. When both are present, the raw hints are authoritative
  for the hint index; `episode_key` is stored only as the legacy association.
- The server writes the hint index in the same transaction as the package. A duplicate package may update its
  hints; a conflicting package never replaces the first accepted tile.
- On a read-only channel node, the write is refused with `403 read_only_channel` and an optional
  `contribute_url`; see below.

## Telegram manifest

Channel storage carries hints in one manifest message per `(fp, profile)`, posted after the last tile:

```
openseek:hints:v1:<base64url(json)>
```

`base64url` is unpadded RFC 4648 §5. The decoded JSON is:

```json
{"v":1,"fp":"<64 hex>","profile":"nvmt-v1-g5","imdb":"tt1234567","season":0,"episode":0,
 "duration_ms":7230000,"size_bytes":1234567890,"filename":"movie.2024.1080p",
 "n_tiles":724,"grid":5000,"created":1760000000}
```

- Caption limit: 900 bytes. If the JSON is larger, truncate `filename` first; never drop
  `v`, `fp`, `profile`, `duration_ms`, or `imdb`.
- `grid` is the profile grid in ms (`5000` or `10000`); `n_tiles` is the package tile count.
- `created` is Unix seconds.
- Tile captions stay `openseek:v1:<fp>:<profile>:<k>:<sha256>`.
- A package without a manifest is exact-`fp`-only. Rebuild never invents hints.
- A manifest that arrives before its tiles is stored but not returned as a candidate until the package exists.

## Read-only channel

A node that can read the channel but cannot post returns:

```
403 {"error":"read_only_channel","contribute_url":"https://host.example"}
```

`contribute_url` is optional. Clients retry the same PUT once against it when present; they never follow an
HTTP redirect. If it is absent, the client surfaces the denial and the user may use the librarian intake.

## Examples

Film, same length:

```
GET /v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms=7234000&size_bytes=1234567890
→ score 100, relation same_length, a_num 1, a_den 1, b_ms 0
```

Series, 24/25 speed shift:

```
GET /v1/pool/candidates?imdb=tt7654321&season=2&episode=4&duration_ms=2700000
→ score 95, relation speed_24_25, a_num 24, a_den 25, b_ms 0
```

Filename fallback, no IMDb:

```
GET /v1/pool/candidates?duration_ms=5400000&filename=some.film.2023.1080p
→ score 75, match "filename+duration", relation same_length
```
