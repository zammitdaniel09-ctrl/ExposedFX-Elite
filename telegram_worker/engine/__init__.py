"""ExposedFX delivery engine: every new post on every route (MAIN and ALT) goes through here.

    model.py    Route, Send/Skip plans, comparable entity and media keys
    rules.py    which posts are never delivered (mentions, links, blocked
                senders, the VIP hard block, forward loops, owner stops)
    replies.py  message-map reads and the reply target in the destination
    render.py   formatter plugins: "copy", "imperium_vip" (VIP topic 7), "translate" (ALT T4T)
    plan.py     Planner: one post on one route -> Send or Skip
    deliver.py  Deliverer: send, verify, retry, record the mapping
    runtime.py  DeliveryEngine wired into the live VIP routes and the owner routes
    intake.py   how new posts reach delivery on MAIN and ALT: pushes, a
                checkpointed watchdog, restart gaps in order, stale posts reported
    mirror.py   edits and deletions mirrored onto copies (MAIN and ALT)
    alt.py      the ALT worker on the engine: translation, ALT reply recovery,
                checkpoints and ALT log markers; ALT's replay audit
    audit.py    replay audit: re-plans recent posts and compares them with
                the copies in the destinations (read-only, once per version)

It replaced the legacy copy pipeline on 4 Nov 2026 after shadow mode (git
history: engine/shadow.py) and a replay audit matched the legacy output on
156/156 delivered posts; ALT followed the same day (76/76, translations included).
"""
