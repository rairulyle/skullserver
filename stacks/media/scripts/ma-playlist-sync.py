"""Add tracks from streaming-provider playlists to the Music Assistant library.

MA only cross-matches tracks that are library items, so tracks played straight
from a Spotify playlist always stream from Spotify even when a higher-quality
local file exists. This script periodically walks all library playlists from
streaming providers and adds their tracks to the library, which triggers MA's
metadata match job and links them to local files where available.
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp
from music_assistant_client.client import MusicAssistantClient

MA_URL = os.environ.get("MA_URL", "http://localhost:8095")
MA_API_TOKEN = os.environ.get("MA_API_TOKEN", "")
INTERVAL = float(os.environ.get("SYNC_INTERVAL_HOURS", "6")) * 3600
# playlists from these providers are skipped: their tracks are already local
SKIP_PROVIDERS = ("builtin", "filesystem")
ADD_TIMEOUT = 180
FETCH_TIMEOUT = 300
ADD_PACING = 3
# consecutive add timeouts mean Spotify's rate limiter is saturated;
# hammering on makes it worse - abort the pass and cool down instead.
# Spotify's Retry-After penalty runs ~64 min, so the cooldown must outlast it.
MAX_CONSECUTIVE_FAILURES = 5
COOLDOWN = 4200
# tracks that keep timing out even after cooldowns are benched so they can't
# masquerade as rate limiting and loop the pass forever
MAX_TRACK_ATTEMPTS = 3
LIBRARY_PAGE_SIZE = 500
STATE_FILE = Path("/data/synced-uris.json")
FAILED_FILE = Path("/data/failed-uris.json")


class RateLimited(Exception):
    """Raised when consecutive add failures indicate provider throttling."""


def log(msg: str) -> None:
    print(msg, flush=True)


def load_state() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def save_state(synced: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(synced)))


def load_failed() -> dict[str, int]:
    try:
        return json.loads(FAILED_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_failed(failed: dict[str, int]) -> None:
    FAILED_FILE.write_text(json.dumps(failed))


async def sync_once() -> None:
    synced = load_state()
    attempts = load_failed()
    async with aiohttp.ClientSession() as session:
        async with MusicAssistantClient(MA_URL, session, token=MA_API_TOKEN) as client:
            # command responses are dispatched by the listen loop; without it
            # running in the background, send_command futures never resolve
            init_ready = asyncio.Event()
            listen_task = asyncio.create_task(client.start_listening(init_ready))
            await asyncio.wait_for(init_ready.wait(), 60)
            try:
                await _sync_playlists(client, synced, attempts)
            finally:
                listen_task.cancel()


async def _get_library_track_keys(client: MusicAssistantClient) -> set[tuple[str, str]]:
    """Collect (provider, item_id) for every track provider-mapping in the library.

    This is the ground truth for "track is done": playlist tracks from streaming
    providers are always returned with their raw provider (never "library"), and
    the synced state file alone is not reliable either, because MA can delete an
    added item later (e.g. as collateral of a recursive album merge).
    """
    keys: set[tuple[str, str]] = set()
    offset = 0
    while True:
        page = await asyncio.wait_for(
            client.music.get_library_tracks(limit=LIBRARY_PAGE_SIZE, offset=offset),
            FETCH_TIMEOUT,
        )
        for track in page:
            for mapping in track.provider_mappings:
                keys.add((mapping.provider_instance, mapping.item_id))
                keys.add((mapping.provider_domain, mapping.item_id))
        if len(page) < LIBRARY_PAGE_SIZE:
            return keys
        offset += LIBRARY_PAGE_SIZE


async def _sync_playlists(
    client: MusicAssistantClient, synced: set[str], attempts: dict[str, int]
) -> None:
    library_keys = await _get_library_track_keys(client)
    log(f"library holds {len(library_keys) // 2} track provider-mappings")
    playlists = await asyncio.wait_for(
        client.music.get_library_playlists(), FETCH_TIMEOUT
    )
    for idx, playlist in enumerate(playlists, 1):
        mapping = next(iter(playlist.provider_mappings), None)
        if mapping is None or mapping.provider_domain.startswith(SKIP_PROVIDERS):
            continue
        log(f"[{idx}/{len(playlists)}] fetching {playlist.name or mapping.item_id}...")
        try:
            tracks = await asyncio.wait_for(
                client.music.get_playlist_tracks(playlist.item_id, "library"),
                FETCH_TIMEOUT,
            )
        except Exception as err:
            log(f"{playlist.name}: could not fetch tracks, skipping ({err!r})")
            continue
        added = readded = failed = benched = consecutive = 0
        try:
            for track in tracks:
                if (
                    track.provider == "library"
                    or (track.provider, track.item_id) in library_keys
                ):
                    attempts.pop(track.uri, None)
                    continue
                if attempts.get(track.uri, 0) >= MAX_TRACK_ATTEMPTS:
                    benched += 1
                    continue
                is_readd = track.uri in synced
                if is_readd:
                    # previously added but no longer library-resolved: re-add,
                    # and count it so a track MA keeps deleting is eventually
                    # benched instead of being re-added on every pass forever
                    # (the counter clears once the track resolves as library)
                    attempts[track.uri] = attempts.get(track.uri, 0) + 1
                    readded += 1
                try:
                    await asyncio.wait_for(
                        client.music.add_item_to_library(track.uri), ADD_TIMEOUT
                    )
                    synced.add(track.uri)
                    # the same track may appear in multiple playlists this pass
                    library_keys.add((track.provider, track.item_id))
                    if not is_readd:
                        attempts.pop(track.uri, None)
                    added += 1
                    consecutive = 0
                    # each add triggers a metadata match; go easy on the APIs
                    await asyncio.sleep(ADD_PACING)
                except (asyncio.TimeoutError, TimeoutError) as err:
                    failed += 1
                    consecutive += 1
                    attempts[track.uri] = attempts.get(track.uri, 0) + 1
                    log(f"  failed to add {track.name} ({track.uri}): {err!r}")
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        raise RateLimited from err
                except Exception as err:
                    failed += 1
                    consecutive = 0
                    attempts[track.uri] = attempts.get(track.uri, 0) + 1
                    log(f"  failed to add {track.name} ({track.uri}): {err!r}")
        finally:
            if added or failed or readded:
                save_state(synced)
                save_failed(attempts)
            if benched:
                log(f"  {benched} track(s) benched after {MAX_TRACK_ATTEMPTS} failed attempts")
            log(
                f"{playlist.name}: {len(tracks)} tracks, "
                f"{added} added to library ({readded} re-added), {failed} failed"
            )


async def main() -> None:
    rate_limited_streak = 0
    while True:
        if not MA_API_TOKEN:
            log(
                "MA_API_TOKEN not set - create a long-lived token in the MA web UI "
                "(profile settings) and set MA_API_TOKEN in the stack .env, "
                "then restart this container. Retrying in 5 minutes."
            )
            await asyncio.sleep(300)
            continue
        try:
            await sync_once()
            rate_limited_streak = 0
        except RateLimited:
            # extended penalties (daily quota) need escalating backoff,
            # or repeated probing keeps the limiter saturated indefinitely
            delay = min(COOLDOWN * 2**rate_limited_streak, 28800)
            rate_limited_streak += 1
            log(f"provider rate limited - cooling down {delay // 60} min, then resuming")
            await asyncio.sleep(delay)
            continue
        except Exception as err:
            log(f"sync failed: {err!r}")
        await asyncio.sleep(INTERVAL)


asyncio.run(main())
