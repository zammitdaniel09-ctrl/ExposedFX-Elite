"""ExposedFX delivery engine.

A small, typed replacement for the legacy copy pipeline (worker_fixed.py's
FASTVIP2 delivery, copy_one/copy_album and the wrappers patched onto them):

    model.py    Route, Send/Skip plans, comparable entity and media keys
    rules.py    which posts are never delivered (mentions, links, blocked
                senders, the VIP hard block, forward loops, owner stops)
    replies.py  message-map reads and the reply target in the destination
    render.py   formatter plugins: "copy" and "imperium_vip" (VIP topic 7)
    plan.py     Planner: one post on one route -> Send or Skip
    shadow.py   shadow mode: plans every live post and compares it with what
                the legacy pipeline actually sent, without sending anything

Routes move to the engine one at a time once shadow mode has matched the
legacy output on live traffic (docs/ARCHITECTURE.md, "Delivery engine").
"""
