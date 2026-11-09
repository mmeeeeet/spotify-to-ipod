#!/usr/bin/env python3
"""
spotify_to_mp3.py — turn a Spotify playlist into a folder of MP3 files.

Spotify's own audio files are DRM-locked and cannot be exported, so this tool
reads the playlist's track list from Spotify, finds each song on YouTube,
downloads the audio, converts it to MP3 (192 kbps) with ffmpeg, and tags it
with the Spotify title/artist.

Usage:
    spotify_to_mp3.py --playlist "<name | spotify URI | open.spotify.com URL>"
                      [--limit N] [--dry-run] [--out DIR]

Requires:
    - the spotify-api CLI (Muse connector) on this machine
    - yt-dlp + mutagen in the .venv next to this script
    - ffmpeg on PATH
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

SPOTIFY_API = "/opt/hatch/bin/spotify-api"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Spotify side
# --------------------------------------------------------------------------

def spotify_call(args):
    """Run the spotify-api CLI and return its parsed JSON output."""
    proc = subprocess.run(
        [SPOTIFY_API] + args,
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "spotify-api %s failed (exit %d): %s"
            % (" ".join(args), proc.returncode, (proc.stderr or proc.stdout)[:400])
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("spotify-api returned non-JSON output: %s" % proc.stdout[:400])


def library_playlists():
    """Return [{'title':..., 'spotify_uri':...}] for the user's playlists."""
    data = spotify_call(["library", "--filter", "PLAYLISTS"])
    out = []
    for section in data.get("sections", []):
        for item in section.get("items", []):
            if item.get("title") and item.get("spotify_uri"):
                out.append({
                    "title": item["title"],
                    "spotify_uri": item["spotify_uri"],
                })
    return out


def resolve_playlist(value):
    """Accept a name, a spotify:playlist: URI, or an open.spotify.com URL.
    Returns (uri, display_hint)."""
    value = value.strip()
    if value.startswith("spotify:"):
        return value, None
    m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", value)
    if m:
        return "spotify:playlist:" + m.group(1), None

    # Otherwise treat it as a playlist name.
    playlists = library_playlists()
    exact = [p for p in playlists if p["title"].strip().lower() == value.lower()]
    if len(exact) == 1:
        return exact[0]["spotify_uri"], exact[0]["title"]
    if len(exact) > 1:
        print("More than one playlist has that name:")
        for p in exact:
            print("  %s  (%s)" % (p["title"], p["spotify_uri"]))
        print("Re-run with the spotify: URI to pick one.")
        sys.exit(2)
    if not playlists:
        print("No playlists found in the Spotify library.")
        sys.exit(2)
    print("No playlist named %r. Your playlists:" % value)
    for p in playlists:
        print("  %s  (%s)" % (p["title"], p["spotify_uri"]))
    sys.exit(2)


def page_items_and_next(data):
    """Pull (items, next_url) out of an experience / next-page response.
    The first page nests items under sections[]; next-page responses carry
    items and next at the top level instead."""
    items, next_url = [], None
    if isinstance(data.get("items"), list):
        items.extend(data["items"])
    for section in data.get("sections", []):
        items.extend(section.get("items", []))
        if section.get("next"):
            next_url = section["next"]
    if data.get("next"):
        next_url = data["next"]
    return items, next_url


def collect_tracks(uri):
    """Fetch every track of a playlist, following pagination.
    Returns (playlist_name, [ {title, artist, spotify_uri, duration_ms} ])."""
    data = spotify_call(["experience", "--id", uri])
    playlist_name = data.get("title") or "playlist"
    items, next_url = page_items_and_next(data)
    seen_next = set()
    while next_url and next_url not in seen_next:
        seen_next.add(next_url)
        data = spotify_call(["next-page", "--url", next_url])
        more, next_url = page_items_and_next(data)
        items.extend(more)

    tracks = []
    for item in items:
        uri_i = item.get("spotify_uri", "")
        if ":track:" not in uri_i:
            continue  # skip episodes / non-track entries
        title = (item.get("title") or "").strip()
        artist = (item.get("subtitle") or "").strip()
        if title:
            tracks.append({
                "title": title,
                "artist": artist,
                "spotify_uri": uri_i,
                "duration_ms": item.get("duration_ms"),
            })
    return playlist_name, tracks


# --------------------------------------------------------------------------
# Download side
# --------------------------------------------------------------------------

def find_ytdlp():
    candidate = os.path.join(SCRIPT_DIR, ".venv", "bin", "yt-dlp")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("yt-dlp")
    if found:
        return found
    return None


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or "track"


def slugify(name):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "playlist"


def tag_mp3(path, title, artist, track_no):
    """Write ID3 title/artist/track number from the Spotify data."""
    from mutagen.id3 import ID3, TIT2, TPE1, TRCK
    from mutagen.id3 import ID3NoHeaderError
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for key in ("TIT2", "TPE1", "TRCK"):
        tags.delall(key)
    tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    tags.add(TRCK(encoding=3, text=str(track_no)))
    tags.save(path)


BOT_CLIENTS = ["android", "tv", "ios"]


def is_bot_wall(text):
    """True when yt-dlp's error output is YouTube's sign-in / bot check."""
    t = (text or "").lower()
    return "not a bot" in t or "sign in to confirm" in t


def probe_duration(path):
    """Real duration of an audio file in seconds (ffprobe), or None."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return float((proc.stdout or "").strip())
    except Exception:
        return None


def audio_hash(path):
    """MD5 hash of the decoded audio stream, or None."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a",
             "-f", "hash", "-hash", "md5", "-"],
            capture_output=True, text=True, timeout=120,
        )
        m = re.search(r"MD5=([0-9a-fA-F]+)", proc.stdout or "")
        return m.group(1).lower() if m else None
    except Exception:
        return None


def duration_ok(file_secs, spotify_secs):
    """Accept when the file's length is within max(8s, 4%) of Spotify's."""
    tolerance = max(8.0, 0.04 * spotify_secs)
    return abs(file_secs - spotify_secs) <= tolerance


def _download_once(ytdlp, url, outtmpl, extra_args):
    """One yt-dlp download attempt. Returns (ok, stderr_text)."""
    cmd = [
        ytdlp,
        "-f", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K",
        "--no-playlist", "--no-warnings", "--quiet", "--no-progress",
    ]
    if extra_args:
        cmd += extra_args
    cmd += ["-o", outtmpl, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "yt-dlp timed out after 600s"
    return proc.returncode == 0, (proc.stderr or "")


def download_candidate(ytdlp, video_id, tmp_base):
    """Download one YouTube video as MP3 next to tmp_base.
    When YouTube throws its bot/sign-in wall, retry through the android,
    tv and ios player clients in that order.
    Returns (produced_path_or_None, error_or_None)."""
    url = "https://www.youtube.com/watch?v=%s" % video_id
    outtmpl = tmp_base + ".%(ext)s"
    produced = tmp_base + ".mp3"
    attempts = [None] + BOT_CLIENTS
    last_err = None
    for idx, client in enumerate(attempts):
        extra = None
        if client:
            extra = ["--extractor-args", "youtube:player_client=%s" % client]
        ok, stderr = _download_once(ytdlp, url, outtmpl, extra)
        if ok and os.path.isfile(produced):
            return produced, None
        err_lines = (stderr or "").strip().splitlines()
        last_err = err_lines[-1] if err_lines else "yt-dlp download failed"
        if os.path.isfile(produced):
            os.remove(produced)
        # Only the bot wall earns the player-client fallback chain.
        if idx == 0 and not is_bot_wall(stderr):
            break
    return None, last_err


def search_candidates(ytdlp, search, limit=3):
    """List up to `limit` YouTube video ids for a search string.
    The bot-wall client fallback applies to the listing too.
    Returns (ids, error_or_None)."""
    attempts = [None] + BOT_CLIENTS
    last_err = None
    for client in attempts:
        cmd = [ytdlp, "--flat-playlist", "--no-warnings", "--quiet",
               "--print", "id"]
        if client:
            cmd += ["--extractor-args", "youtube:player_client=%s" % client]
        cmd.append(search)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return [], "yt-dlp search timed out after 120s"
        ids = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if ids:
            return ids[:limit], None
        err_lines = (proc.stderr or "").strip().splitlines()
        last_err = err_lines[-1] if err_lines else "yt-dlp search exit %d" % proc.returncode
        if not is_bot_wall(proc.stderr):
            break
    return [], last_err


def query_variants(track):
    """Search strings for a track, most specific first. YouTube search
    silently returns zero results for some over-specified queries (long
    featured-artist lists, feat. parentheticals), so fall back to
    simpler forms before giving up."""
    artist = track["artist"]
    title = track["title"]
    first_artist = artist.split(",")[0].strip()
    bare_title = re.sub(r"\s*\((?:feat\.|ft\.)[^)]*\)\s*", " ", title,
                        flags=re.I).strip()
    variants = [
        "%s - %s" % (artist, title),
        "%s - %s" % (first_artist, bare_title),
        "%s %s" % (first_artist, bare_title),
    ]
    out = []
    for v in variants:
        if v not in out:
            out.append(v)
    return out


def fetch_track(ytdlp, track, tmp_dir, seen_hashes):
    """Walk the top YouTube candidates for one track, applying the guards:
    a candidate is accepted only if its real duration matches Spotify's
    (max(8s, 4%) tolerance) and its decoded-audio hash has not already
    been accepted for another track in this run.
    Returns a dict: {ok, path, query, spotify_secs, file_secs, hash, error}."""
    spotify_secs = None
    if track.get("duration_ms"):
        spotify_secs = track["duration_ms"] / 1000.0

    cand_ids, list_err, search = [], None, None
    for q in query_variants(track):
        cand_ids, list_err = search_candidates(ytdlp, "ytsearch3:%s" % q)
        if cand_ids:
            search = "ytsearch3:%s" % q
            break
    if not cand_ids:
        return {"ok": False,
                "query": "ytsearch3:%s" % query_variants(track)[0],
                "error": list_err or "no YouTube candidates found"}

    saw_download = False
    saw_duration_pass = False
    saw_dup = False
    last_err = None
    for cand_no, video_id in enumerate(cand_ids, 1):
        tmp_base = os.path.join(tmp_dir, "cand%d" % cand_no)
        produced, err = download_candidate(ytdlp, video_id, tmp_base)
        if not produced:
            last_err = err
            continue
        saw_download = True
        file_secs = probe_duration(produced)
        if spotify_secs is not None:
            if file_secs is None or not duration_ok(file_secs, spotify_secs):
                os.remove(produced)
                continue
            saw_duration_pass = True
        h = audio_hash(produced)
        if h is not None and h in seen_hashes:
            saw_dup = True
            os.remove(produced)
            continue
        return {"ok": True, "path": produced, "query": search,
                "spotify_secs": spotify_secs, "file_secs": file_secs,
                "hash": h, "error": None}

    if not saw_download:
        error = last_err or "download failed"
    elif spotify_secs is not None and not saw_duration_pass:
        error = "no match passed the duration check"
    elif saw_dup:
        error = "every candidate duplicated another track"
    else:
        error = last_err or "no usable candidate"
    return {"ok": False, "query": search, "error": error}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Download a Spotify playlist as tagged MP3 files (via YouTube matching)."
    )
    ap.add_argument("--playlist", required=True,
                    help="Playlist name, spotify:playlist: URI, or open.spotify.com URL")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N tracks")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the track list and exit; download nothing")
    ap.add_argument("--out", default=None,
                    help="Output folder (default: output/<playlist-slug>/ next to this script)")
    args = ap.parse_args(argv)

    uri, _hint = resolve_playlist(args.playlist)
    playlist_name, tracks = collect_tracks(uri)
    if args.limit is not None:
        tracks = tracks[:args.limit]

    print("Playlist: %s" % playlist_name)
    print("Tracks:   %d" % len(tracks))

    if args.dry_run:
        for i, t in enumerate(tracks, 1):
            print("%3d. %s - %s" % (i, t["artist"], t["title"]))
        print("\nDry run: nothing downloaded.")
        return 0

    if not tracks:
        print("No tracks to download.")
        return 0

    ytdlp = find_ytdlp()
    if not ytdlp:
        print("ERROR: yt-dlp not found. Create the venv and install it:")
        print("  cd %s && python3 -m venv .venv && .venv/bin/pip install yt-dlp mutagen" % SCRIPT_DIR)
        return 1
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH.")
        return 1

    out_dir = args.out or os.path.join(SCRIPT_DIR, "output", slugify(playlist_name))
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = os.path.join(out_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    width = max(2, len(str(len(tracks))))
    downloaded, skipped, failed = [], [], []
    checks = []        # (label, spotify_secs, file_secs) per downloaded track
    seen_hashes = {}   # decoded-audio hash -> label, for the duplicate guard

    for i, track in enumerate(tracks, 1):
        label = "%s - %s" % (track["artist"], track["title"])
        fname = "%0*d - %s.mp3" % (width, i, sanitize(label)[:150])
        dest = os.path.join(out_dir, fname)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print("[%d/%d] skip (exists): %s" % (i, len(tracks), label), flush=True)
            h = audio_hash(dest)
            if h is not None and h not in seen_hashes:
                seen_hashes[h] = label
            skipped.append(label)
            continue
        print("[%d/%d] downloading: %s" % (i, len(tracks), label), flush=True)
        result = fetch_track(ytdlp, track, tmp_dir, seen_hashes)
        if not result["ok"]:
            print("        FAILED: %s" % result["error"], flush=True)
            failed.append((label, result["query"], result["error"]))
            continue
        shutil.move(result["path"], dest)
        if result["hash"] is not None:
            seen_hashes[result["hash"]] = label
        checks.append((label, result["spotify_secs"], result["file_secs"]))
        try:
            tag_mp3(dest, track["title"], track["artist"], i)
        except Exception as exc:  # tagging is best-effort; the audio matters most
            print("        warning: could not write ID3 tags (%s)" % exc, flush=True)
        downloaded.append(label)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    report_path = os.path.join(out_dir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("Playlist: %s\n" % playlist_name)
        fh.write("Downloaded: %d\nSkipped (already existed): %d\nFailed: %d\n\n"
                 % (len(downloaded), len(skipped), len(failed)))
        if downloaded:
            fh.write("DOWNLOADED\n")
            for label in downloaded:
                fh.write("  %s\n" % label)
            fh.write("\n")
        if checks:
            fh.write("CHECKS (spotify duration vs file duration)\n")
            for label, spotify_secs, file_secs in checks:
                if spotify_secs is not None and file_secs is not None:
                    fh.write("  %s: spotify %.1fs vs file %.1fs\n"
                             % (label, spotify_secs, file_secs))
                elif file_secs is not None:
                    fh.write("  %s: spotify duration unknown, file %.1fs\n"
                             % (label, file_secs))
                else:
                    fh.write("  %s: duration could not be probed\n" % label)
            fh.write("\n")
        if skipped:
            fh.write("SKIPPED\n")
            for label in skipped:
                fh.write("  %s\n" % label)
            fh.write("\n")
        if failed:
            fh.write("FAILED (search query used shown for each)\n")
            for label, query, err in failed:
                fh.write("  %s\n    query: %s\n    error: %s\n" % (label, query, err))

    print("\nDone. Downloaded %d, skipped %d, failed %d."
          % (len(downloaded), len(skipped), len(failed)))
    print("Folder: %s" % out_dir)
    print("Report: %s" % report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
