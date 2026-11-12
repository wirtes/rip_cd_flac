#!/usr/bin/env python3
"""Rip an audio CD on macOS to tagged FLAC files.

Install: brew install libcdio-paranoia libdiscid flac
Run: python3 rip_cd_flac.py --contact you@example.com
MusicBrainz receives the disc ID and the contact in the HTTP User-Agent.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def read_disc(device):
    prefix = subprocess.check_output(
        ["brew", "--prefix", "libdiscid"], text=True
    ).strip()
    lib = ctypes.CDLL(str(Path(prefix) / "lib/libdiscid.dylib"))
    signatures = {
        "discid_new": (ctypes.c_void_p, []),
        "discid_free": (None, [ctypes.c_void_p]),
        "discid_get_default_device": (ctypes.c_char_p, []),
        "discid_read_sparse": (
            ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        ),
        "discid_get_error_msg": (ctypes.c_char_p, [ctypes.c_void_p]),
        "discid_get_id": (ctypes.c_char_p, [ctypes.c_void_p]),
        "discid_get_first_track_num": (ctypes.c_int, [ctypes.c_void_p]),
        "discid_get_last_track_num": (ctypes.c_int, [ctypes.c_void_p]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments

    drive = os.fsencode(device) if device else lib.discid_get_default_device()
    if not drive:
        raise ValueError("No CD drive found. Insert an audio CD or pass --device.")
    disc = lib.discid_new()
    if not disc:
        raise ValueError("Could not initialize libdiscid.")
    try:
        if not lib.discid_read_sparse(disc, drive, 0):
            raise ValueError(lib.discid_get_error_msg(disc).decode(errors="replace"))
        return (
            os.fsdecode(drive), lib.discid_get_id(disc).decode("ascii"),
            lib.discid_get_first_track_num(disc), lib.discid_get_last_track_num(disc),
        )
    finally:
        lib.discid_free(disc)


def artist_credit(item):
    return "".join(
        credit["name"] + credit.get("joinphrase", "")
        for credit in item.get("artist-credit", [])
    )


def lookup(disc_id, count, contact):
    query = urlencode({"fmt": "json", "cdstubs": "no",
                       "inc": "recordings+artist-credits+discids"})
    request = Request(
        f"https://musicbrainz.org/ws/2/discid/{disc_id}?{query}",
        headers={"User-Agent": f"rip-cd-flac/1.0 ({contact})"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            releases = json.load(response).get("releases", [])
    except HTTPError as error:
        if error.code != 404:
            raise
        releases = []

    matches = [
        (release, medium)
        for release in releases
        for medium in release.get("media", [])
        if any(disc["id"] == disc_id for disc in medium.get("discs", []))
        and len(medium.get("tracks", [])) == count
    ]
    if not matches:
        raise ValueError(
            f"No exact MusicBrainz track-list match for {disc_id}. "
            "Use --manual to enter the tags yourself."
        )
    if len(matches) == 1:
        return matches[0]

    for index, (release, medium) in enumerate(matches, 1):
        print(f"{index}. {artist_credit(release)} – {release['title']} "
              f"({release.get('date') or '?'}, {release.get('country') or '?'}, "
              f"disc {medium['position']}, barcode {release.get('barcode') or '?'})")
        print(f"   https://musicbrainz.org/release/{release['id']}")
    selection = input("Select the release number: ").strip()
    if not selection.isdecimal() or not 1 <= int(selection) <= len(matches):
        raise ValueError("Invalid release number.")
    return matches[int(selection) - 1]


def manual_metadata(count):
    artist = input("Album artist: ").strip()
    album = input("Album title: ").strip()
    if not artist or not album:
        raise ValueError("Album artist and title are required.")
    date = input("Release year (optional): ").strip()
    tracks = []
    for position in range(1, count + 1):
        title = input(f"Track {position:02d} title: ").strip()
        track_artist = input("  Track artist (Enter for album artist): ").strip()
        tracks.append({
            "position": position, "title": title or f"Track {position:02d}",
            "artist-credit": [{"name": track_artist or artist}],
        })
    return {"title": album, "date": date, "artist-credit": [{"name": artist}]}, {
        "position": 1, "tracks": tracks,
    }


def safe_name(value):
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", value)
    # Leave room for the file extension and keep multibyte names within NAME_MAX.
    return cleaned.encode("utf-8")[:200].decode("utf-8", "ignore").strip(" .") or "_"


def rip(drive, first, last, release, medium, output):
    album = release["title"]
    album_artist = artist_credit(release) or "Unknown Artist"
    tracks = sorted(medium["tracks"], key=lambda track: track["position"])
    count = last - first + 1
    if [track["position"] for track in tracks] != list(range(1, count + 1)):
        raise ValueError("The metadata track positions don't match the audio tracks.")
    disc_number = medium["position"]
    disc_total = len(release.get("media") or [medium])
    album_dir = output / safe_name(album_artist) / safe_name(album)
    destination = album_dir / f"Disc {disc_number:02d}" if disc_total > 1 else album_dir
    if os.path.lexists(destination):
        raise FileExistsError(f"Output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".rip-", dir=destination.parent) as temp:
        work = Path(temp)
        encoded = work / "flac"
        encoded.mkdir()
        # Abort a track on an uncorrectable skip. Batch WAVs are numbered by the CD.
        subprocess.run(
            ["cd-paranoia", "-d", drive, "-X", "-B", f"{first}-{last}"],
            cwd=work, check=True,
        )
        wav_files = {}
        for wav in work.glob("*.wav"):
            match = re.fullmatch(r"track(\d+)\..*wav", wav.name)
            if match:
                wav_files[int(match.group(1))] = wav
        if set(wav_files) != set(range(first, last + 1)):
            raise ValueError("The rip did not produce a WAV for every expected track.")

        for cd_number, track in zip(range(first, last + 1), tracks):
            position = track["position"]
            recording = track.get("recording") or {}
            title = track.get("title") or recording.get("title") or f"Track {position:02d}"
            artist = artist_credit(track) or artist_credit(recording) or album_artist
            tags = {
                "ALBUM": album, "ALBUMARTIST": album_artist, "TITLE": title,
                "ARTIST": artist, "TRACKNUMBER": position, "TRACKTOTAL": count,
                "DISCNUMBER": disc_number, "DISCTOTAL": disc_total,
                "DATE": release.get("date"), "MUSICBRAINZ_ALBUMID": release.get("id"),
                "MUSICBRAINZ_TRACKID": recording.get("id"),
            }
            filename = encoded / f"{position:02d} - {safe_name(title)}.flac"
            command = ["flac", "-5", "--verify", "-o", str(filename)]
            command += [f"--tag={key}={value}" for key, value in tags.items() if value]
            subprocess.run([*command, str(wav_files[cd_number])], check=True)

        # Reserve the final directory without overwriting an existing album.
        destination.mkdir()
        try:
            encoded.replace(destination)
        except OSError:
            destination.rmdir()
            raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path.home() / "Music/CD-Rips")
    parser.add_argument("--device", help="Optical drive device, e.g. /dev/disk4")
    parser.add_argument("--contact", help="Email or URL for the MusicBrainz User-Agent")
    parser.add_argument("--manual", action="store_true", help="Enter tags without MusicBrainz")
    args = parser.parse_args()
    if not args.manual and not args.contact:
        parser.error("--contact is required for MusicBrainz lookup (or use --manual).")

    try:
        for tool in ("brew", "cd-paranoia", "flac"):
            if not shutil.which(tool):
                raise ValueError(f"{tool} is missing. Install the prerequisites with Homebrew.")
        drive, disc_id, first, last = read_disc(args.device)
        count = last - first + 1
        print(f"Drive: {drive} | MusicBrainz disc ID: {disc_id} | Audio tracks: {count}", flush=True)
        release, medium = (
            manual_metadata(count) if args.manual else lookup(disc_id, count, args.contact)
        )
        print(f"Ripping {release['title']} (disc {medium['position']})", flush=True)
        print(f"Saved to {rip(drive, first, last, release, medium, args.output.expanduser().resolve())}")
    except (OSError, ValueError, EOFError, subprocess.CalledProcessError) as error:
        print(f"Rip failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRip cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
