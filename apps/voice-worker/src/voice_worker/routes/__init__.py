"""HTTP routes the voice worker serves besides the media socket.

The worker's job is the audio path. Everything here exists because the
panel needs a window on the real pipeline -- the same retriever, the same
agent, the same voice -- rather than a demo of a similar one. Kept out of
``main`` so the entry module stays what a deploy reads first: lifespan,
health, the socket.
"""
