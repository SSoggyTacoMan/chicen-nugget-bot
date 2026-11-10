"""Nugget facts: tiny bits of branded delight."""

from __future__ import annotations

import random

from bot.slack_api import post

FACTS = (
    "the chicken nugget was popularised in the 1950s by Robert C. Baker, a Cornell food scientist.",
    "nuggets are usually made from ground meat, not a single cut — shaping is part of the magic.",
    "the word 'nugget' originally meant a small lump of native metal, usually gold.",
    "a nugget's crispy shell is a Maillard reaction: sugars and proteins browning together.",
    "the average chicken nugget is roughly 15–20% breading, depending on who you ask.",
    "nuggets freeze well, which is a big reason they became a supermarket staple.",
    "the first frozen chicken nuggets hit US grocery shelves in the early 1960s.",
    "nugget shapes vary by market: stars, dinosaurs, letters, and the classic boot-ish blob.",
    "the nugget-to-sauce ratio is a serious field of personal research.",
    "a 4-piece nugget order is a snack; a 20-piece is a personality.",
    "nuggets are a delivery vehicle for sauce, and that is valid.",
    "the plural 'nuggets' is also slang for small useful pieces of knowledge. like this one.",
    "nuggets are best friends with waffle fries and honey mustard.",
    "a cold nugget is a tragedy; a reheated nugget is a comeback story.",
    "the crispiness scale runs from 'sad soggy' to 'structurally impressive'.",
    "nuggets have a long history in school cafeterias and childhood nostalgia.",
    "the ideal nugget has a crunchy edge, a soft middle, and no dry patches.",
    "nuggets are technically a formed food product, which makes them engineering.",
    "there is no wrong dipping sauce, only stronger and weaker opinions.",
    "a nugget in a burger is just a chicken sandwich with extra confidence.",
    "the nugget emoji doesn't exist yet, which is a global oversight.",
    "this bot is named after a nugget and takes that responsibility seriously.",
    "nuggets pair well with pickles, hot honey, ranch, and chaos.",
    "if you drop a nugget, the five-second rule applies only to the brave.",
    "the best nugget is the one someone else ordered and you get to steal.",
)


def random_fact() -> str:
    return random.choice(FACTS)


async def cmd_nugget(ack, command, client) -> None:
    await ack()
    await post(
        client,
        command["channel_id"],
        f":chicken: *nugget fact:* {random_fact()}",
    )


__all__ = ["FACTS", "random_fact"]
