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
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


RELEASE_LINK_TYPES = {"release", "release-group", "artist", "recording", "label", "track", "area", "collection"}


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


def musicbrainz_lookup(path, query, contact):
    request = Request(
        f"https://musicbrainz.org/ws/2/{path}?{query}",
        headers={"User-Agent": f"rip-cd-flac/1.0 ({contact})"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            if error.code != 404:
                raise ValueError(
                    f"MusicBrainz lookup failed for {path} ({error}). "
                    "Use --manual to enter the tags yourself."
                ) from error
        return None


def disc_releases(disc_id, contact):
    # The disc ID endpoint includes media and disc IDs automatically.
    query = "fmt=json&cdstubs=no&inc=recordings+artist-credits+release-groups"
    result = musicbrainz_lookup(f"discid/{disc_id}", query, contact) or {}
    return result.get("releases", [])


def lookup(disc_id, count, contact):

    matches = [
        (release, medium)
        for release in disc_releases(disc_id, contact)
        for medium in release.get("media", [])
        if any(disc["id"] == disc_id for disc in medium.get("discs", []))
        and len(medium.get("tracks", [])) == count
    ]
    if not matches:
        raise ValueError(
            f"No exact MusicBrainz track-list match for {disc_id}. "
            "Use --url with a MusicBrainz link or --manual to enter tags."
        )
    return choose_medium(matches)


def choose_medium(matches):
    if len(matches) == 1:
        return matches[0]

    for index, (release, medium) in enumerate(matches, 1):
        disc_title = medium.get("title") or medium["tracks"][0].get("title")
        disc_label = f"disc {medium['position']}"
        if disc_title:
            disc_label += f" · {disc_title}"
        print(f"{index}. {artist_credit(release)} – {release['title']} "
              f"({release.get('date') or '?'}, {release.get('country') or '?'}, "
              f"{disc_label}, barcode {release.get('barcode') or '?'})")
        print(f"   https://musicbrainz.org/release/{release['id']}")
    selection = input("Select the release and disc by list number: ").strip()
    if not selection.isdecimal() or not 1 <= int(selection) <= len(matches):
        raise ValueError("Invalid release number.")
    return matches[int(selection) - 1]


def musicbrainz_url(value):
    message = "Expected a MusicBrainz URL (https://musicbrainz.org/…)."
    try:
        url = urlsplit(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(message) from error
    if url.scheme not in {"http", "https"} or url.netloc.lower() not in {
        "musicbrainz.org", "www.musicbrainz.org", "beta.musicbrainz.org",
    }:
        raise argparse.ArgumentTypeError(message)
    path = url.path[6:] if url.path.startswith("/ws/2/") else url.path
    path = path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2:
        entity, identifier = parts[:2]
        if entity in {"cdtoc", "discid"} and re.fullmatch(r"[A-Za-z0-9._-]{28}", identifier):
            return "discid", identifier
        if entity in RELEASE_LINK_TYPES and re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier, re.IGNORECASE,
        ):
            return entity, identifier.lower()
    raise argparse.ArgumentTypeError(
        "Use a MusicBrainz release, release group, artist, recording, label, track, "
        "area, public release collection, or disc ID page."
    )


def releases_for_url(entity, identifier, contact):
    query = "fmt=json&inc=recordings+artist-credits+discids+release-groups"
    if entity == "release":
        release = musicbrainz_lookup(f"release/{identifier}", query, contact)
        if release is None:
            raise ValueError(f"MusicBrainz release {identifier} was not found.")
        yield release
        return
    if entity == "discid":
        yield from disc_releases(identifier, contact)
        return

    offset = 0
    while True:
        page = musicbrainz_lookup(
            "release", f"{query}&{entity}={identifier}&limit=100&offset={offset}", contact,
        ) or {}
        releases = page.get("releases", [])
        yield from releases
        offset += len(releases)
        if not releases or offset >= page.get("release-count", 0):
            return
        time.sleep(1)


def lookup_url(entity, identifier, disc_id, count, contact):
    candidates = []
    for release in releases_for_url(entity, identifier, contact):
        for medium in release.get("media", []):
            tracks = medium.get("tracks", [])
            if len(tracks) != count:
                continue
            if entity == "track" and not any(track.get("id") == identifier for track in tracks):
                continue
            if entity == "recording" and not any(
                (track.get("recording") or {}).get("id") == identifier for track in tracks
            ):
                continue
            candidates.append((release, medium))

    if not candidates:
        raise ValueError(f"No disc with a {count}-track list was found for that MusicBrainz URL. "
                         "Check the URL or use --manual to enter tags.")
    exact = [(release, medium) for release, medium in candidates
             if any(disc["id"] == disc_id for disc in medium.get("discs", []))]
    return choose_medium(exact or candidates)


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


def download_cover(release, destination, contact):
    sources = (
        ("release", release.get("id")),
        ("release-group", (release.get("release-group") or {}).get("id")),
    )
    for entity, identifier in sources:
        if not identifier:
            continue
        request = Request(
            f"https://coverartarchive.org/{entity}/{identifier}/front-500",
            headers={"User-Agent": f"rip-cd-flac/1.0 ({contact})"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                limit = 4 * 1024 * 1024
                image = response.read(limit + 1)
        except HTTPError as error:
            with error:
                if error.code != 404:
                    print(f"Cover art lookup failed for {entity}: {error}", file=sys.stderr)
            continue
        except (OSError, ValueError) as error:
            print(f"Cover art lookup failed for {entity}: {error}", file=sys.stderr)
            continue
        if len(image) <= limit and image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"):
            cover = destination / "cover.jpg"
            cover.write_bytes(image)
            return cover
    return None


def rip(device, first, last, release, medium, output, contact=None):
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
        command = ["cd-paranoia"]
        # libdiscid's default on macOS is a drive number; cd-paranoia auto-detects its drive.
        if device:
            command.extend(["-d", device])
        command.extend(["-X", "-B", f"{first}-{last}"])
        subprocess.run(command, cwd=work, check=True)
        wav_files = {}
        for wav in work.glob("*.wav"):
            match = re.fullmatch(r"track(\d+)\..*wav", wav.name)
            if match:
                wav_files[int(match.group(1))] = wav
        if set(wav_files) != set(range(first, last + 1)):
            raise ValueError("The rip did not produce a WAV for every expected track.")

        cover = download_cover(release, encoded, contact) if contact and release.get("id") else None
        if contact and release.get("id") and cover is None:
            print("Front cover unavailable. Saving audio and tags without cover art.", file=sys.stderr)
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
            if cover:
                command.append(f"--picture={cover}")
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
    metadata = parser.add_mutually_exclusive_group()
    metadata.add_argument("--manual", action="store_true", help="Enter tags without MusicBrainz")
    metadata.add_argument("--url", "--release", metavar="URL", type=musicbrainz_url,
                          help="Find release tags from a MusicBrainz URL")
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
        if args.manual:
            release, medium = manual_metadata(count)
        elif args.url:
            release, medium = lookup_url(*args.url, disc_id, count, args.contact)
        else:
            release, medium = lookup(disc_id, count, args.contact)
        print(f"Ripping {release['title']} (disc {medium['position']})", flush=True)
        destination = rip(args.device, first, last, release, medium,
                          args.output.expanduser().resolve(), args.contact)
        print(f"Saved to {destination}")
    except (OSError, ValueError, EOFError, subprocess.CalledProcessError) as error:
        print(f"Rip failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRip cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
