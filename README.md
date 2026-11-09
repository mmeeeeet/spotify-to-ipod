# Spotify → iPod (MP3)

Turn any Spotify playlist into a folder of MP3 files you can put on an
iPod Shuffle (or anything else that plays MP3s and has no Spotify/internet).

**How it works, honestly:** Spotify's own audio files are DRM-locked and
cannot be exported. So this tool reads the playlist's track list from
Spotify, finds each song on YouTube, downloads the audio, converts it to
MP3 at 192 kbps, and tags it with the Spotify title, artist, and track
number. Filenames keep the playlist order: `01 - Artist - Title.mp3`.

## Setup

```sh
cd spotify-to-ipod
python3 -m venv .venv
.venv/bin/pip install yt-dlp mutagen
```

Also needed on the machine:

- the `spotify-api` CLI (Muse Spotify connector), with Spotify connected
- `ffmpeg` on PATH

## Usage

Always run it with the venv's Python (that's where yt-dlp/mutagen live):

```sh
# by playlist name (exact match, case-insensitive)
.venv/bin/python spotify_to_mp3.py --playlist "Music to Learn French By"

# by Spotify URI
.venv/bin/python spotify_to_mp3.py --playlist "spotify:playlist:1M8abjWQN5j9P5QD0plUiE"

# by open.spotify.com link
.venv/bin/python spotify_to_mp3.py --playlist "https://open.spotify.com/playlist/…"

# just list the tracks, download nothing
.venv/bin/python spotify_to_mp3.py --playlist "…" --dry-run

# only the first N tracks (good for a test)
.venv/bin/python spotify_to_mp3.py --playlist "…" --limit 5

# choose the output folder
.venv/bin/python spotify_to_mp3.py --playlist "…" --out ~/Music/for-the-ipod
```

Default output: `output/<playlist-name>/` next to the script.

Notes:

- **Resumable:** tracks whose MP3 already exists in the output folder are
  skipped, so re-running after an interruption only fetches what's missing.
- **`report.txt`** lands in the output folder after every run: what
  downloaded, what was skipped, and what failed — for failures it shows the
  exact YouTube search query used, so you can see why a match went wrong.
- **Matching caveat:** each song is matched by a YouTube search for
  "Artist - Title". Obscure tracks occasionally match a live version or a
  cover — check `report.txt` and give the list a quick listen.
- **Built-in guards:** every download is checked before it is tagged —
  its real length must match Spotify's track length (within 8s / 4%),
  its audio must not duplicate a song already fetched in the same run,
  and if YouTube throws its "confirm you're not a bot" wall the download
  retries through other player clients automatically. A song no
  candidate passes for is reported as failed, never silently mis-tagged;
  `report.txt` shows the Spotify-vs-file duration for every download.
- **Liked Songs** aren't supported (Spotify's connector can't open the
  Liked Songs collection as a playlist). Workaround: put the songs in a
  regular playlist and point the tool at that.

## Putting the songs on an iPod Shuffle

Dragging MP3 files straight onto the Shuffle in a file browser does **not**
work — the iPod only plays songs that iTunes/the Music app has indexed
onto it. The sync takes two minutes:

1. Open **iTunes** (Windows) or the **Music** app (Mac).
2. Add the output folder to the library: *File → Add Folder/File to Library*
   (Mac: *File → Import…*).
3. Plug in the iPod Shuffle — it appears in the sidebar.
4. Select the Shuffle, go to its **Music** sync settings, tick the playlist
   or songs you added, and hit **Sync / Apply**.
5. Wait for the sync to finish, then eject the Shuffle before unplugging.

On a Mac running macOS Catalina or later, the same sync lives in
**Finder**: plug in the Shuffle, select it under *Locations*, open the
*Music* tab, choose what to sync, and click *Apply*.

## License

MIT — take it, fork it, make it yours.
