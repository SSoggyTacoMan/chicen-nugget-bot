"""Small async wrappers around the Slack Web API with consistent error handling."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


async def is_private_host(hostname: str) -> bool:
    if not hostname:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except OSError:
        return True
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


async def ssrf_filter(request: httpx.Request) -> None:
    host = request.url.host
    if not host or await is_private_host(host):
        raise ValueError(f"blocked request to private or loopback host: {host}")



def error_code(exc: Exception) -> str:
    if isinstance(exc, SlackApiError):
        try:
            return exc.response.get("error") or str(exc)
        except Exception:  # noqa: BLE001
            return str(exc)
    return str(exc)


async def post(
    client: AsyncWebClient,
    channel: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    unfurl_links: bool = False,
) -> dict[str, Any] | None:
    try:
        response = await client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=unfurl_links,
            unfurl_media=unfurl_links,
        )
        return dict(response.data) if response.data else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_postMessage failed for %s: %s", channel, error_code(exc))
        return None


async def update(
    client: AsyncWebClient,
    channel: str,
    ts: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> bool:
    try:
        await client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_update failed for %s/%s: %s", channel, ts, error_code(exc))
        return False


async def post_ephemeral(
    client: AsyncWebClient,
    channel: str,
    user: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> bool:
    try:
        await client.chat_postEphemeral(channel=channel, user=user, text=text, blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("chat_postEphemeral failed for %s, falling back to DM: %s", user, error_code(exc))
        return bool(await post(client, user, text, blocks=blocks))


async def react(client: AsyncWebClient, channel: str, ts: str, emoji: str) -> None:
    try:
        await client.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except Exception:  # noqa: BLE001 - reactions are a nicety
        pass


async def unreact(client: AsyncWebClient, channel: str, ts: str, emoji: str) -> None:
    try:
        await client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
    except Exception:  # noqa: BLE001
        pass


async def user_info(client: AsyncWebClient, user_id: str) -> dict[str, Any] | None:
    try:
        response = await client.users_info(user=user_id)
        return response.get("user")
    except Exception as exc:  # noqa: BLE001
        logger.info("users_info failed for %s: %s", user_id, error_code(exc))
        return None


async def user_label(client: AsyncWebClient, user_id: str) -> str:
    user = await user_info(client, user_id)
    if not user:
        return f"<@{user_id}>"
    profile = user.get("profile") or {}
    name = profile.get("display_name") or user.get("real_name") or user.get("name") or user_id
    return f"{name} (<@{user_id}>)"


async def channel_info(client: AsyncWebClient, channel_id: str) -> dict[str, Any] | None:
    try:
        response = await client.conversations_info(channel=channel_id)
        return response.get("channel")
    except Exception as exc:  # noqa: BLE001
        logger.info("conversations_info failed for %s: %s", channel_id, error_code(exc))
        return None


async def ensure_public_member(client: AsyncWebClient, channel_id: str) -> str:
    """Join a public channel if needed. Returns member/joined/private/failed/missing_scope."""
    info = await channel_info(client, channel_id)
    if not info:
        return "failed"
    if info.get("is_member"):
        return "member"
    if info.get("is_private") or info.get("is_archived"):
        return "private"
    try:
        await client.conversations_join(channel=channel_id)
        return "joined"
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code == "missing_scope":
            return "missing_scope"
        logger.warning("conversations_join failed for %s: %s", channel_id, code)
        return "failed"


async def all_channels(client: AsyncWebClient) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    cursor = None
    try:
        while True:
            response = await client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            channels.extend(response.get("channels") or [])
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        logger.info("conversations_list failed: %s", error_code(exc))
    return channels


async def all_users(client: AsyncWebClient) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    cursor = None
    try:
        while True:
            response = await client.users_list(limit=200, cursor=cursor)
            users.extend(response.get("members") or [])
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        logger.info("users_list failed: %s", error_code(exc))
    return users


async def usergroup_members(client: AsyncWebClient, group_id: str) -> list[str] | None:
    try:
        response = await client.usergroups_users_list(usergroup=group_id)
        return list(response.get("users") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("usergroups_users_list failed: %s", error_code(exc))
        return None


async def ensure_usergroup_member(client: AsyncWebClient, group_id: str, user_id: str) -> str:
    members = await usergroup_members(client, group_id)
    if members is None:
        return "failed"
    if user_id in members:
        return "already"
    try:
        await client.usergroups_users_update(usergroup=group_id, users=",".join(members + [user_id]))
        return "added"
    except Exception as exc:  # noqa: BLE001
        logger.warning("usergroups_users_update failed: %s", error_code(exc))
        return "failed"


async def ensure_usergroup_not_member(client: AsyncWebClient, group_id: str, user_id: str) -> str:
    members = await usergroup_members(client, group_id)
    if members is None:
        return "failed"
    if user_id not in members:
        return "not_member"
    remaining = [member for member in members if member != user_id]
    if not remaining:
        logger.warning("refusing to empty usergroup %s", group_id)
        return "failed"
    try:
        await client.usergroups_users_update(usergroup=group_id, users=",".join(remaining))
        return "removed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("usergroups_users_update failed: %s", error_code(exc))
        return "failed"


async def is_channel_member(
    client: AsyncWebClient, channel_id: str, user_id: str
) -> bool | None:
    """True/False, or None when membership can't be checked."""
    cursor = None
    try:
        while True:
            response = await client.conversations_members(
                channel=channel_id, limit=1000, cursor=cursor
            )
            if user_id in (response.get("members") or []):
                return True
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return False
    except Exception as exc:  # noqa: BLE001
        logger.info("conversations_members failed for %s: %s", channel_id, error_code(exc))
        return None


async def invite_to_channel(
    client: AsyncWebClient, channel_id: str, user_id: str
) -> tuple[bool, str | None]:
    """Returns (ok, error). `already_in_channel` counts as success."""
    try:
        await client.conversations_invite(channel=channel_id, users=user_id)
        return True, None
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code == "already_in_channel":
            return True, None
        logger.warning("conversations_invite failed for %s in %s: %s", user_id, channel_id, code)
        return False, code


async def fetch_slack_images(event: dict[str, Any], token: str, max_images: int = 3) -> list[str]:
    files = event.get("files") or []
    if not files and isinstance(event.get("message"), dict):
        files = event["message"].get("files") or []

    images = [
        f
        for f in files
        if isinstance(f, dict)
        and str(f.get("mimetype", "")).startswith("image/")
        and "svg" not in str(f.get("mimetype", ""))
    ][:max_images]

    data_urls: list[str] = []
    if images and token:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            for f in images:
                url = f.get("url_private_download") or f.get("url_private")
                mimetype = f.get("mimetype", "image/png")
                size = f.get("size", 0)
                if not url or (size and size > 15 * 1024 * 1024):
                    continue
                try:
                    resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
                    if resp.status_code == 200 and resp.content:
                        b64 = base64.b64encode(resp.content).decode("ascii")
                        data_urls.append(f"data:{mimetype};base64,{b64}")
                    else:
                        logger.warning("failed to fetch slack image %s: HTTP %s", url, resp.status_code)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("exception fetching slack image %s: %s", url, exc)

    if not data_urls:
        text = event.get("text") or ""
        if not text and isinstance(event.get("message"), dict):
            text = event["message"].get("text") or ""
        img_pattern = r'https?://[^\s<>\"\'\)\|]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^\s<>\"\'\)\|]*)?'
        found_urls = re.findall(img_pattern, text, re.IGNORECASE)[:max_images]
        if found_urls:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, event_hooks={"request": [ssrf_filter]}) as http:
                for img_url in found_urls:
                    parsed = urlparse(img_url)
                    host = (parsed.hostname or "").lower()
                    if not host or await is_private_host(host):
                        continue
                    try:
                        resp = await http.get(img_url)
                        if resp.status_code == 200 and resp.content and len(resp.content) <= 10 * 1024 * 1024:
                            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                            if not ctype.startswith("image/"):
                                ext = parsed.path.rsplit(".", 1)[-1].lower()
                                ctype = f"image/{ext}" if ext in {"png", "jpg", "jpeg", "webp", "gif"} else "image/png"
                                if ctype == "image/jpg":
                                    ctype = "image/jpeg"
                            b64 = base64.b64encode(resp.content).decode("ascii")
                            data_urls.append(f"data:{ctype};base64,{b64}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("exception fetching image url %s: %s", img_url, exc)

    return data_urls
