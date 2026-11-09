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
    Returns (playlist_name, [ {title, artist, spotify_uri} ])."""
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
            tracks.append({"title": title, "artist": artist, "spotify_uri": uri_i})
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


def download_track(ytdlp, track, dest_path, tmp_dir):
    """Download one track as MP3 to dest_path. Returns (ok, query, error)."""
    query = "ytsearch1:%s - %s" % (track["artist"], track["title"])
    outtmpl = os.path.join(tmp_dir, "dl.%(ext)s")
    cmd = [
        ytdlp,
        "-f", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K",
        "--no-playlist", "--no-warnings", "--quiet", "--no-progress",
        "-o", outtmpl,
        query,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, query, "yt-dlp timed out after 600s"
    produced = os.path.join(tmp_dir, "dl.mp3")
    if proc.returncode != 0 or not os.path.isfile(produced):
        err = (proc.stderr or "").strip().splitlines()
        return False, query, err[-1] if err else "yt-dlp exit %d" % proc.returncode
    shutil.move(produced, dest_path)
    return True, query, None


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

    for i, track in enumerate(tracks, 1):
        label = "%s - %s" % (track["artist"], track["title"])
        fname = "%0*d - %s.mp3" % (width, i, sanitize(label)[:150])
        dest = os.path.join(out_dir, fname)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print("[%d/%d] skip (exists): %s" % (i, len(tracks), label), flush=True)
            skipped.append(label)
            continue
        print("[%d/%d] downloading: %s" % (i, len(tracks), label), flush=True)
        ok, query, err = download_track(ytdlp, track, dest, tmp_dir)
        if not ok:
            print("        FAILED: %s" % err, flush=True)
            failed.append((label, query, err))
            continue
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
