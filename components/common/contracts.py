"""Event extras shared between components.

Components talk to each other through these keys on an ``AstrMessageEvent``.
Keep every key here so a rename never silently breaks another component.
The ``*_v1`` values are kept unchanged for compatibility with saved receipts.
"""

# A synthetic event built by proactive chat. Media components send through it
# and must not treat it as an incoming user message.
PROACTIVE_DELIVERY = "life_delivery_v1"
# The event supports a confirmed photo delivery acknowledgement.
PHOTO_ACK = "life_photo_ack_v1"
# Voice receipts (list of {"id", "text", "medium"}) recorded after a real send.
VOICE_DELIVERY = "life_voice_delivery_v1"
# A photo job re-entered the pipeline to finish its reply.
PHOTO_COMPLETION = "life_photo_completion"
# Set by the chat component for a normal reply in a target private chat.
NORMAL_CHAT = "life_private_normal_reply_v1"
# Facts about unanswered proactive messages, consumed by the reply guidance.
WAIT_REPLY_FACTS = "life_private_wait_reply_facts_v1"
# Per-event caches (see runtime.component_enabled and schedule snapshots).
ENABLED_CACHE = "_life_enabled_cache"
STATE_CACHE = "_life_state_cache"
