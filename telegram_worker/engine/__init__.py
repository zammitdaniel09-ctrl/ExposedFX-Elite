"""ExposedFX delivery engine: every new post on every MAIN route goes through here.

    model.py    Route, Send/Skip plans, comparable entity and media keys
    rules.py    which posts are never delivered (mentions, links, blocked
                senders, the VIP hard block, forward loops, owner stops)
    replies.py  message-map reads and the reply target in the destination
    render.py   formatter plugins: "copy" and "imperium_vip" (VIP topic 7)
    plan.py     Planner: one post on one route -> Send or Skip
    deliver.py  Deliverer: send, verify, retry, record the mapping
    runtime.py  DeliveryEngine wired into FASTVIP2 and the owner routes
    audit.py    replay audit: re-plans recent posts and compares them with
                the copies in the destinations (read-only, once per version)

It replaced the legacy copy pipeline on 4 Nov 2026 after shadow mode (git
history: engine/shadow.py) and a replay audit matched the legacy output on
156/156 delivered posts.
"""
