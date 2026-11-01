"""Versioned transcript contract. No transcript bytes are written outside Cosmos."""

import hashlib
import html
import json
import math
import re
import struct
from datetime import datetime, timezone

import tiktoken

DIMENSIONS = 505
PROFILE = "vtt-v1:cl100k:600:100:800:text-embedding-3-large:505"
ENCODING = tiktoken.get_encoding("cl100k_base")
MAX_PAGE_BYTES = 128 * 1024
STAMP = r"[+-]?(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
TIMING = re.compile(rf"^({STAMP})\s+-->\s+({STAMP})(?:\s+.*)?$")
VOICE = re.compile(r"<v(?:\.[^ >]+)?\s+([^>]+)>(.*?)(?:</v>|$)", re.S)


class CorpusError(RuntimeError):
    """Only fixed, non-sensitive classifications cross the runtime boundary."""


def unix(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def utc(seconds):
    return (
        datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def hash_id(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def remaining(expires_at, now):
    ttl = unix(expires_at) - int(now.timestamp())
    if ttl <= 0:
        raise CorpusError("transcript_expired")
    return ttl


def offset(value):
    sign = -1 if value.startswith("-") else 1
    parts = value.lstrip("+-").split(":")
    seconds = float(parts[-1]) + 60 * int(parts[-2])
    if len(parts) == 3:
        seconds += 3600 * int(parts[0])
    return sign * round(seconds * 1000)


def plain(value):
    # Decode entities after removing markup so literal escaped tags remain data.
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def parse_vtt(raw):
    if len(raw) > 16 * 1024 * 1024:
        raise CorpusError("transcript_download_too_large")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise CorpusError("invalid_transcript_encoding") from None
    if not text.startswith("WEBVTT"):
        raise CorpusError("unsupported_transcript_format")
    utterances = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if lines and lines[0].startswith("WEBVTT"):
            # Graph can put the first cue directly after its header.
            cue = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if cue is None:
                continue
            lines = lines[cue:]
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        index = 0 if "-->" in lines[0] else 1
        if index >= len(lines) or not (match := TIMING.fullmatch(lines[index])):
            raise CorpusError("invalid_transcript_cue")
        start, end = match.groups()
        if offset(end) < offset(start):
            raise CorpusError("invalid_transcript_offset")
        payload = "\n".join(lines[index + 1 :])
        voices = list(VOICE.finditer(payload))
        pieces = []
        cursor = 0
        for voice in voices:
            if plain(payload[cursor : voice.start()]):
                pieces.append((None, payload[cursor : voice.start()]))
            pieces.append((plain(voice[1]), voice[2]))
            cursor = voice.end()
        if plain(payload[cursor:]):
            pieces.append((None, payload[cursor:]))
        for speaker, part in pieces:
            content = plain(part)
            if content:
                utterances.append(
                    dict(
                        index=len(utterances),
                        text=content,
                        speaker=speaker,
                        start=start,
                        end=end,
                        startMs=offset(start),
                        endMs=offset(end),
                    )
                )
    if not utterances:
        raise CorpusError("transcript_empty")
    return utterances


def pages(utterances):
    """Keep Unicode text intact; split exceptionally large cues into located parts."""
    current = []
    for utterance in utterances:
        # Bound a single part by UTF-8 bytes, then measure the actual JSON page envelope.
        parts = []
        pending = utterance["text"]
        while pending:
            take = min(len(pending), 16_000)
            parts.append(pending[:take])
            pending = pending[take:]
        for number, part in enumerate(parts):
            item = dict(utterance, text=part, part=number, parts=len(parts))
            candidate = current + [item]
            if (
                len(json.dumps(candidate, ensure_ascii=False).encode())
                > MAX_PAGE_BYTES - 4096
            ):
                yield current
                current = []
            current.append(item)
    if current:
        yield current


def chunks(utterances):
    """Split only on Unicode character boundaries, carrying exact cue/text locators."""
    segments = []
    for u in utterances:
        pending, position = u["text"], 0
        while pending:
            # A long cue is split without decoding incomplete UTF-8 token sequences.
            take = min(len(pending), 400)
            if len(ENCODING.encode(pending[:take])) > 90:
                lo, hi = 1, take
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if len(ENCODING.encode(pending[:mid])) <= 90:
                        lo = mid
                    else:
                        hi = mid - 1
                take = lo
            segments.append(
                dict(
                    u, text=pending[:take], charStart=position, charEnd=position + take
                )
            )
            position += take
            pending = pending[take:]
    cursor = 0
    while cursor < len(segments):
        stop = cursor
        content = ""
        while stop < len(segments):
            u = segments[stop]
            line = f"[{u['start']}–{u['end']}] {u['speaker'] + ': ' if u['speaker'] else ''}{u['text']}"
            candidate = content + ("\n" if content else "") + line
            count = len(ENCODING.encode(candidate))
            if count > 800:
                if stop == cursor:
                    raise CorpusError("transcript_label_too_large")
                break
            content = candidate
            stop += 1
            if count >= 600:
                break
        yield dict(
            text=content,
            locators=[
                {
                    k: u[k]
                    for k in (
                        "index",
                        "charStart",
                        "charEnd",
                        "start",
                        "end",
                        "speaker",
                    )
                }
                for u in segments[cursor:stop]
            ],
        )
        next_cursor = stop
        overlap = 0
        while next_cursor > cursor + 1:
            size = len(ENCODING.encode(segments[next_cursor - 1]["text"]))
            if overlap + size > 100:
                break
            overlap += size
            next_cursor -= 1
        cursor = next_cursor


def embedding_vector(values):
    if not isinstance(values, list) or len(values) != DIMENSIONS:
        raise CorpusError("invalid_embedding_dimensions")
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            raise ValueError
        vector = [struct.unpack("f", struct.pack("f", v))[0] for v in values]
        if not all(math.isfinite(v) for v in vector) or not any(vector):
            raise ValueError
    except (ValueError, OverflowError, struct.error):
        raise CorpusError("invalid_embedding_values") from None
    return vector
