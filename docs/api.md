# API

The full contract is [openapi.json](openapi.json). Regenerate it after a
change with `python -m ev_journal.api > docs/openapi.json`; a test fails when
it is out of date, and every response in the tests is checked against it.

The API runs inside `ev-journal loop` when `EVJ_API_TOKEN_FILE` is set. It
listens on `EVJ_API_HOST:EVJ_API_PORT` (default `0.0.0.0:8080`). Publish it
only on the private network.

## Auth

Every request needs `Authorization: Bearer <token>`. The token is the content
of `EVJ_API_TOKEN_FILE`, outside the repo. Without a configured token every
request gets `401`.

## Places and routes

| method | path | does |
|---|---|---|
| `GET` | `/places` | all places; with `?changed_since=T` the changed ones and delete markers |
| `POST` | `/places` | create; the server picks the id |
| `GET` | `/places/{id}` | one place |
| `PUT` | `/places/{id}` | change |
| `DELETE` | `/places/{id}` | delete (leaves a delete marker for sync) |
| `GET` | `/places/suggestions` | frequent stops with no place |

Routes (`/routes`, `/routes/{id}`) work the same. A route is a name and an
ordered list of existing place ids.

**Sync.** `GET …?changed_since=T` returns the rows changed at or after `T`
(ms), deleted ones as `deleted` markers, and `now`. Use `now` as the next `T`.
A row may come twice; that is harmless.

**Versions.** Every write returns the new `version`. Send the version you saw
in the body (`PUT`) or as `?version=` (`DELETE`). There is one user, so the
last write wins. When the version you sent was old, the answer has
`"stale": true`, so the client can tell the owner.

**Seed.** `EVJ_PLACES_FILE` is imported once, into an empty store. After that
the API owns the places.

## Suggestions

Journey ends (stops of more than 30 min) that lie in no place are grouped
within 100 m. A group with 3 or more visits is a suggestion: position, visits
and the median local hour of arrival. To name one, `POST /places` at its
position; it then stops being suggested. Suggestions come from the last
recompute, so they are empty until the first run is done.

## Journey labels

`PUT /journeys/{id}/label` with `{"label": "commute" | "other" | null,
"not_me": bool}`. The id is a time in ms, usually the journey's start; the
label attaches to the journey that contains that time, so it survives every
recompute. `null` means "use the computed label". `ev_journeys` then has
`label_by_hand` and `not_me`.

## Command log

`POST /commands` with `client_id`, `time`, `text`, `action` and an optional
`place_id` or `route_id`. The client queues offline and sends later. The first
post of a `client_id` returns `201`; a repeat returns `200` with the stored
command and changes nothing.
