# Rip CD to FLAC on macOS

`rip_cd_flac.py` rips an audio CD into one tagged FLAC file per track. It uses `cd-paranoia` to read the disc, `libdiscid` to identify it, MusicBrainz to find the release and track information, and `flac` to encode the audio.

## Setup

You'll need macOS, an optical drive that reads audio CDs, Python 3, and Homebrew. From the folder containing `rip_cd_flac.py`, run:

```sh
brew install libcdio-paranoia libdiscid flac
python3 --version
```

The script uses Python's standard library. To run it directly as `./rip_cd_flac.py`, make it executable once:

```sh
chmod +x rip_cd_flac.py
```

You can also use `python3 rip_cd_flac.py` in place of `./rip_cd_flac.py` in every example below.

## Rip a CD with MusicBrainz tags

Insert an audio CD and run the command with your email address or a contact URL:

```sh
./rip_cd_flac.py --contact 'you@example.com'
```

Replace the example address with your own. The script sends the disc ID to MusicBrainz and includes your contact in the request's User-Agent header. A MusicBrainz account is optional and there is no login step. If multiple releases match, enter the number for the edition you have.

By default, files go under `~/Music/CD-Rips/Album Artist/Album/`, with names such as `01 - First Track.flac`. Matched multi-disc releases get a folder for each disc, such as `Disc 02`. The script writes album and track titles, artist credits, track and disc numbers, and the release date when available.

## Choose an output folder

To save the rip to a Lyrion music folder that is mounted on your Mac, substitute the actual path to the mount:

```sh
./rip_cd_flac.py --contact 'you@example.com' --output '/Volumes/Music Library'
```

The script creates the artist and album folders under the path you supply. After the rip, rescan your music library in Lyrion if the album doesn't appear automatically.

## Use a MusicBrainz link

If automatic lookup finds no exact match, pass a MusicBrainz link with `--url`:

```sh
./rip_cd_flac.py --contact 'you@example.com' \
  --url 'https://musicbrainz.org/release/65268362-7186-4cf8-a3ed-78ceed054039'
```

The script accepts release, release group, artist, recording, label, track, area, public release collection, and disc ID links. `--release` also works as an alias for `--url`. The script requires the same number of tracks on the CD and the selected disc. It prefers a matching disc ID when available and asks you to choose if multiple editions or discs qualify. Check that the release has the same track order as your CD before using its tags.

## Cover art

When using MusicBrainz tags, the script looks for a front cover in the Cover Art Archive. It tries the selected release first, then the release group. When it finds a cover, it saves `cover.jpg` alongside the tracks and embeds the image in each FLAC. The rip can complete if artwork is unavailable.

## Enter tags manually

If MusicBrainz has no exact match, or you want to enter your own tags, run:

```sh
./rip_cd_flac.py --manual
```

The script prompts for album artist, album title, optional release year, and each track's title and artist. Press Return at the track artist prompt to use the album artist.

## Select a specific drive

If automatic drive detection picks the wrong drive, use `diskutil list` to find the optical disc's device. Substitute that device for `/dev/disk4`:

```sh
diskutil list
./rip_cd_flac.py --contact 'you@example.com' --device /dev/disk4
```

## Troubleshooting

- **`--contact is required`** — Supply `--contact 'you@example.com'` for MusicBrainz lookup or use `--manual`.
- **`Permission denied` when running the script directly** — Run `chmod +x rip_cd_flac.py` or invoke it with `python3 rip_cd_flac.py`.
- **`Output already exists`** — Choose a different output root with `--output` if you want to keep both copies.
- **See all options** — Run `./rip_cd_flac.py --help`.
