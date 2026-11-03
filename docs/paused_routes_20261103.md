# Paused routes — 3 Nov 2026

Owner request: stop all forwarding into the Hub "ExposedFX VIP"
(`-1003918958200`) and the Partner group "ExposedFX ELITE" (`-1004442967052`),
keeping every route on record for later. Later the same day the owner also
removed "Jamie SUB IB" (`-1003802436353`) and "Market Slayers forwarding"
(`-1004365831817`), which the MAIN account could no longer reach. Market
Slayers forwarding ran from 27 Aug 2026 until the account lost access on
28 Oct 2026 (`ChannelPrivateError`).

**Nothing was deleted.** The route definitions remain in
`telegram_worker/routes.py`; message maps, checkpoints and state files on the
MAIN volume are untouched. The pause is enforced in
`telegram_worker/owner_forwarding_policy.py`:

- `STOPPED_DEST_CHATS` drops these routes from `ROUTES` at startup.
- `install_send_guard` refuses any send, forward or edit into either chat on
  every code path of the MAIN client (live routes, watchdogs, one-time
  backfills, topic seeding, sidecars). Reads and deletes are not blocked.

## How to restore

Remove the chat id (`-1003918958200`, `-1004442967052`, `-1003802436353` or
`-1004365831817`)
from `STOPPED_DEST_CHATS`,
then deploy MAIN. Routes resume from their saved checkpoints. Any route that
was stopped at runtime is also listed in `/app/data/owner_stopped_forwarding_20261102.json`;
remove its entry there too if it should resume.

## Live routes paused (45)

| # | Route | Source chat | Source topic | Destination | Dest topic |
|---|---|---|---|---|---|
| 1 | Premium I Live Trade | `-1003814307529` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 25 |
| 2 | Market Slayers VIP | `-1003838973021` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 23 |
| 3 | Dropout VIP | `-1003840418063` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 22 |
| 4 | MANJOX TRADES | `-1003887896696` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 31 |
| 5 | BOLZEGHA VIP | `-1002900239477` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 35 |
| 6 | ELX Premium | `-1003393003521` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 36 |
| 7 | KEY / ALCHEMIST 1 | `-1003681070311` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 28 |
| 8 | MSC Premium | `-1003909296106` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 34 |
| 9 | Master Premium | `-1003457399744` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 20 |
| 10 | ExposedFX Route 362 | `-1002444443378` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 362 |
| 11 | Route569 2 | `-1003860051934` | 2 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 12 | Route569 155 | `-1003860051934` | 155 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 13 | Route569 1 | `-1003860051934` | 1 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 14 | Route569 439 | `-1003860051934` | 439 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 15 | Route569 116 | `-1003860051934` | 116 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 16 | Route569 543 | `-1003860051934` | 543 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 17 | Route569 5 | `-1003860051934` | 5 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 18 | Route569 54 | `-1003860051934` | 54 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 19 | Route569 156 | `-1003860051934` | 156 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 20 | Route569 209 | `-1003860051934` | 209 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 21 | Route569 243 | `-1003860051934` | 243 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 22 | Route569 272 | `-1003860051934` | 272 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 23 | Route569 331 | `-1003860051934` | 331 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 24 | Route569 363 | `-1003860051934` | 363 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 25 | Route569 412 | `-1003860051934` | 412 | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 26 | Route570 | `-1003671770570` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 570 |
| 27 | Mirror Route569 3812195730 | `-1003812195730` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 569 |
| 28 | Mirror Route26892 3651353503 | `-1003651353503` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 26892 |
| 29 | Mirror Route28464 3087047858 | `-1003087047858` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 28464 |
| 30 | Mirror Route29327 3770573057 | `-1003770573057` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 29327 |
| 31 | Mirror Route26902 2186832814 | `-1002186832814` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 26902 |
| 32 | Mirror Route568 3894275482 | `-1003894275482` | whole chat | ExposedFX VIP (Hub) `-1003918958200` | 568 |
| 33 | Jamie SUB IB FORWARDING | 11 To 31 | `-1003726286301` | 11 | Jamie SUB IB `-1003802436353` | 31 |
| 34 | Jamie SUB IB FORWARDING | 7 To 35 | `-1003726286301` | 7 | Jamie SUB IB `-1003802436353` | 35 |
| 35 | Jamie SUB IB FORWARDING | 13 To 33 | `-1003726286301` | 13 | Jamie SUB IB `-1003802436353` | 33 |
| 36 | Jamie SUB IB FORWARDING | 5 To 37 | `-1003726286301` | 5 | Jamie SUB IB `-1003802436353` | 37 |
| 37 | Market Slayers FORWARDING | 11 To 33 | `-1003726286301` | 11 | Market Slayers forwarding `-1004365831817` | 33 |
| 38 | Market Slayers FORWARDING | 7 To 32 | `-1003726286301` | 7 | Market Slayers forwarding `-1004365831817` | 32 |
| 39 | Market Slayers FORWARDING | 13 To 31 | `-1003726286301` | 13 | Market Slayers forwarding `-1004365831817` | 31 |
| 40 | Relay2583 To Hub68237 | `-1004367822325` | 2583 | ExposedFX VIP (Hub) `-1003918958200` | 68237 |
| 41 | Relay2585 To Partner2 | `-1004367822325` | 2585 | ExposedFX ELITE (Partner) `-1004442967052` | 2 |
| 42 | Relay508 To Partner9 | `-1004367822325` | 508 | ExposedFX ELITE (Partner) `-1004442967052` | 9 |
| 43 | Relay115 To Partner11 | `-1004367822325` | 115 | ExposedFX ELITE (Partner) `-1004442967052` | 11 |
| 44 | Relay2 To Partner15 | `-1004367822325` | 2 | ExposedFX ELITE (Partner) `-1004442967052` | 15 |
| 45 | MANJOX To Partner20 | `-1003887896696` | whole chat | ExposedFX ELITE (Partner) `-1004442967052` | 20 |

## Background jobs that also wrote to these chats

| Job | Contract | Notes |
|---|---|---|
| `partner_routepack_last100.py` | relay 2583/2585/508/115/2, MANJOX, `-1002385852838`/8 → Partner/Hub topics | One-time last-100 import |
| `force_82617_last50.py` | `-1002385852838`/62159 → Hub/82617 | Source already owner-stopped |
| `requested_route_bootstrap.py` | Hub topic bootstraps | Started from `runtime_guard.py` |
| `worker_fixed.seed_empty_destination_topics_last50` | Seeds empty Hub topics | Startup task |
| `worker_signal_hub.py` | Hub signal posts | Service already offline |

## Exact route definitions

```json
[
 {
  "dest_chat": -1003918958200,
  "dest_topic": 25,
  "name": "Premium I Live Trade",
  "source_chat": -1003814307529,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 23,
  "name": "Market Slayers VIP",
  "source_chat": -1003838973021,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 22,
  "name": "Dropout VIP",
  "source_chat": -1003840418063,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 31,
  "name": "MANJOX TRADES",
  "source_chat": -1003887896696,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 35,
  "name": "BOLZEGHA VIP",
  "source_chat": -1002900239477,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 36,
  "name": "ELX Premium",
  "source_chat": -1003393003521,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 28,
  "name": "KEY / ALCHEMIST 1",
  "source_chat": -1003681070311,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 34,
  "name": "MSC Premium",
  "source_chat": -1003909296106,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 20,
  "name": "Master Premium",
  "source_chat": -1003457399744,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 362,
  "name": "ExposedFX Route 362",
  "source_chat": -1002444443378,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 2",
  "source_chat": -1003860051934,
  "source_topic": 2
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 155",
  "source_chat": -1003860051934,
  "source_topic": 155
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 1",
  "source_chat": -1003860051934,
  "source_topic": 1
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 439",
  "source_chat": -1003860051934,
  "source_topic": 439
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 116",
  "source_chat": -1003860051934,
  "source_topic": 116
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 543",
  "source_chat": -1003860051934,
  "source_topic": 543
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 5",
  "source_chat": -1003860051934,
  "source_topic": 5
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 54",
  "source_chat": -1003860051934,
  "source_topic": 54
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 156",
  "source_chat": -1003860051934,
  "source_topic": 156
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 209",
  "source_chat": -1003860051934,
  "source_topic": 209
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 243",
  "source_chat": -1003860051934,
  "source_topic": 243
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 272",
  "source_chat": -1003860051934,
  "source_topic": 272
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 331",
  "source_chat": -1003860051934,
  "source_topic": 331
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 363",
  "source_chat": -1003860051934,
  "source_topic": 363
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Route569 412",
  "source_chat": -1003860051934,
  "source_topic": 412
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 570,
  "name": "Route570",
  "source_chat": -1003671770570,
  "source_topic": null
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 569,
  "name": "Mirror Route569 3812195730",
  "source_chat": -1003812195730,
  "source_topic": null,
  "verify_title": true
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 26892,
  "name": "Mirror Route26892 3651353503",
  "source_chat": -1003651353503,
  "source_topic": null,
  "verify_title": true
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 28464,
  "name": "Mirror Route28464 3087047858",
  "source_chat": -1003087047858,
  "source_topic": null,
  "verify_title": true
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 29327,
  "name": "Mirror Route29327 3770573057",
  "source_chat": -1003770573057,
  "source_topic": null,
  "verify_title": false
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 26902,
  "name": "Mirror Route26902 2186832814",
  "source_chat": -1002186832814,
  "source_topic": null,
  "verify_title": false
 },
 {
  "dest_chat": -1003918958200,
  "dest_topic": 568,
  "name": "Mirror Route568 3894275482",
  "source_chat": -1003894275482,
  "source_topic": null,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1003802436353,
  "dest_topic": 31,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1003802436353
  ],
  "name": "Jamie SUB IB FORWARDING | 11 To 31",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 11,
  "strict_source_topic": true,
  "sub_ib_forwarding": "Jamie SUB IB FORWARDING",
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1003802436353,
  "dest_topic": 35,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1003802436353
  ],
  "name": "Jamie SUB IB FORWARDING | 7 To 35",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 7,
  "strict_source_topic": true,
  "sub_ib_forwarding": "Jamie SUB IB FORWARDING",
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1003802436353,
  "dest_topic": 33,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1003802436353
  ],
  "name": "Jamie SUB IB FORWARDING | 13 To 33",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 13,
  "strict_source_topic": true,
  "sub_ib_forwarding": "Jamie SUB IB FORWARDING",
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1003802436353,
  "dest_topic": 37,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1003802436353
  ],
  "name": "Jamie SUB IB FORWARDING | 5 To 37",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 5,
  "strict_source_topic": true,
  "sub_ib_forwarding": "Jamie SUB IB FORWARDING",
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004365831817,
  "dest_topic": 33,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1004365831817
  ],
  "market_slayers_forwarding": "Market Slayers FORWARDING",
  "name": "Market Slayers FORWARDING | 11 To 33",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 11,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004365831817,
  "dest_topic": 32,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1004365831817
  ],
  "market_slayers_forwarding": "Market Slayers FORWARDING",
  "name": "Market Slayers FORWARDING | 7 To 32",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 7,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004365831817,
  "dest_topic": 31,
  "live_only": true,
  "loop_guard_peers": [
   -1003726286301,
   -1004365831817
  ],
  "market_slayers_forwarding": "Market Slayers FORWARDING",
  "name": "Market Slayers FORWARDING | 13 To 31",
  "ordered_private": true,
  "source_chat": -1003726286301,
  "source_topic": 13,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1003918958200,
  "dest_topic": 68237,
  "name": "Relay2583 To Hub68237",
  "source_chat": -1004367822325,
  "source_topic": 2583,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004442967052,
  "dest_topic": 2,
  "name": "Relay2585 To Partner2",
  "source_chat": -1004367822325,
  "source_topic": 2585,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004442967052,
  "dest_topic": 9,
  "name": "Relay508 To Partner9",
  "source_chat": -1004367822325,
  "source_topic": 508,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004442967052,
  "dest_topic": 11,
  "name": "Relay115 To Partner11",
  "source_chat": -1004367822325,
  "source_topic": 115,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004442967052,
  "dest_topic": 15,
  "name": "Relay2 To Partner15",
  "source_chat": -1004367822325,
  "source_topic": 2,
  "strict_source_topic": true,
  "verify_title": false
 },
 {
  "anonymous_send_as": false,
  "copy_reply_parent": false,
  "dest_chat": -1004442967052,
  "dest_topic": 20,
  "name": "MANJOX To Partner20",
  "source_chat": -1003887896696,
  "source_topic": null,
  "verify_title": false
 }
]
```
