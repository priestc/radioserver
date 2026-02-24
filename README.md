# RadioServer

A personal radio station that streams your music library to an iOS app. It scans your local music collection, builds a smart playlist with artist/genre/decade variety rules, and serves tracks to the RadioClient iOS app over your local network or remotely via Tailscale.

## Features

- Scans and indexes your music library (MP3, FLAC, M4A, AAC, OGG, Opus, WAV, WMA)
- Smart playlist generation with configurable skip rules (artist, genre, decade)
- Auto-generates playlist when running low during client sync
- Album cover art extraction and serving
- Low-bitrate transcoding (128kbps) for cellular data usage via ffmpeg
- API key authentication
- QR code display for easy API key entry on the iOS app
- Django admin interface for managing artists, albums, tracks, and playlist settings

## Installation

### Prerequisites

- Python 3.9+
- ffmpeg (for low-bitrate transcoding)
- pipx

### Install

```
pipx install --force git+https://github.com/priestc/radioserver.git
```

### Configure

Create `~/.radioserver.conf`:

```ini
[library]
path = /path/to/your/music
```

The music directory should be organized as `Artist/Album/Track.mp3`.

### Initialize the database

```
radioserver migrate
radioserver createsuperuser
```

### Scan your music library

```
radioserver scan
```

Use `radioserver scan --force` to re-read tags for all files regardless of modification time.

### Generate an initial playlist

```
radioserver generate_playlist 8
```

This generates 8 hours of playlist. The server also auto-generates playlist during client sync when the unplayed queue drops below 1 hour.

### Create an API key

Log into the admin at `http://your-server:9437/admin/`, go to **Api keys**, and add a new key. A QR code is displayed for easy scanning from the iOS app.

### Run the server

For quick testing:

```
radioserver runserver 0.0.0.0:9437
```

For production, use gunicorn:

```
radioserver gunicorn radioserver.wsgi:application --bind 0.0.0.0:9437
```

## Running as a systemd service

Install, enable, and start the service automatically:

```
sudo radioserver install_service
```

Check status:

```
sudo systemctl status radioserver
```

View logs:

```
journalctl -u radioserver -f
```

Edit `radioserver.service` if your Python path or working directory differs from the defaults.

## Remote access with Tailscale

RadioServer works great with [Tailscale](https://tailscale.com/) for remote access, especially behind CGNAT (e.g. Starlink). Install Tailscale on both your server and iPhone, then use the Tailscale IP as the remote server address in the iOS app.

## iOS App (RadioClient)

The Xcode project is in `ios/RadioClient/`. Open it in Xcode and build to your device.

### Settings

The app has two server address fields:

- **Local IP** - Used when connected to WiFi (e.g. `192.168.1.50:9437`)
- **Remote IP** - Used when on cellular data (e.g. `100.64.0.1:9437` for Tailscale)

The app automatically switches between them based on your network connection.

Enter the API key manually or tap the QR code scanner button to scan it from the admin page.

### Playback behavior

- Songs sync at 50% playback and report played status to the server
- On WiFi: downloads and caches songs at full quality up to the configured buffer size
- On cellular: downloads one song at a time at 128kbps, keeping at most 2 songs cached
- Skipped tracks are recorded and reported to the server

## API Endpoints

All API endpoints require a Bearer token in the `Authorization` header.

- `POST /library/api/client_sync/` - Sync playback data and get download list
- `GET /library/api/download_song/<id>/` - Download a song at original quality
- `GET /library/api/download_song_lowbitrate/<id>/` - Download a song transcoded to 128kbps MP3
- `GET /library/cover/<album_id>/` - Get album cover art

## AI Date Finder

Look up release years for tracks using AI:

```
radioserver ai_date_finder 6297 --backend groq --dry-run
```

Supports multiple AI backends (Claude, Groq, DeepSeek, OpenAI, Google). See [AI_BACKENDS.md](AI_BACKENDS.md) for setup instructions, API key links, and free tier details.

## Admin

The Django admin at `/admin/` lets you manage:

- **Artists** - Exclude artists from playlist generation
- **Albums** - Edit metadata, upload cover art, exclude from playlists
- **Tracks** - View and manage individual tracks
- **Genre Groups** - Group genres together for playlist skip rules
- **Playlist Settings** - Configure artist/genre/decade skip counts
- **Playlist Items** - View the generated playlist and playback history
- **API Keys** - Create and manage API keys (with QR codes)
