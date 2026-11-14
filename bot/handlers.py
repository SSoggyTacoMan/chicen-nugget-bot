from __future__ import annotations

import logging

from bot.features import access, agent, polls, recaps, router, setup, shortcuts, threads, welcomer

logger = logging.getLogger(__name__)


def register(app) -> None:
    app.event("message")(agent.on_message)
    app.event("app_mention")(agent.on_app_mention)
    app.event("reaction_added")(agent.on_reaction_added)

    welcomer.register(app)
    access.register(app)
    polls.register(app)
    threads.register(app)
    recaps.register(app)
    setup.register(app)
    router.register(app)
    shortcuts.register(app)

    app.error(_on_error)


async def _on_error(error, body, logger) -> None:
    logger.error("unhandled bolt error: %s", error, exc_info=error)
