# AriaCast Server

**High-performance, low-latency PCM audio streaming over WebSockets with Music Assistant integration.**

AriaCast Server is a lightweight, bidirectional audio streaming receiver written in Python. It transforms any device (Windows, Linux, macOS, Raspberry Pi) into a network "speaker" capable of receiving raw PCM audio, synchronizing metadata in real-time, and offering remote volume control.

Now featuring **Music Assistant plugin integration** and a **modern, artwork-enhanced web player** with glassmorphism UI and dynamic backgrounds.

## ✨ Features

### 🎵 Core Audio Streaming
- **Hybrid Audio Transport**:
  - **WebSocket Stream**: Low-latency raw PCM transmission (48kHz/16-bit default)
  - **HTTP Stream**: `/stream.wav` endpoint for compatibility with generic players (VLC, Browser)
- **Adaptive Buffering**: Smart server-side buffering to handle network jitter
- **Cross-Platform**: Core logic in Python with platform-specific extensions

### 🎛️ Control & Metadata
- **Bidirectional Control**:
  - Native system volume control (Windows `pycaw` support with PowerShell fallback)
  - Real-time metadata synchronization (Title, Artist, Album, Artwork, Position, Duration)
- **Artwork Support**: Automatic downloading and serving of album artwork
- **Progress Tracking**: Real-time playback position and duration

### 🔍 Discovery & Integration
- **Auto-Discovery**: Dual-stack discovery using Multicast DNS (Bonjour/ `_audiocast._tcp`) and custom UDP broadcast
- **Music Assistant Plugin**: Full integration as a Music Assistant provider with source selection and control
- **Web Dashboard**: Modern, responsive web player with artwork backgrounds and visualizer

### 🎨 Enhanced Web Player
- **Glassmorphism UI**: Modern frosted glass design with backdrop blur
- **Dynamic Backgrounds**: Album artwork automatically becomes the page background
- **Spectrum Visualizer**: Colorful real-time audio spectrum analysis
- **Responsive Design**: Optimized for desktop, tablet, and mobile
- **Real-time Stats**: Buffer monitoring, sample rate, and connection status

## 🚀 Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/yourusername/ariacast-server.git
   cd ariacast-server
   ```

2. **Install dependencies**:
   Ensure you have Python 3.9+ installed.
   ```bash
   pip install -r requirements.txt
   ```
   *Note: On Windows, some build tools might be required for `pycaw` or `sounddevice` depending on your environment. On Linux, you may need `libportaudio2`.*

## 🎯 Usage

### Standalone Server

Start the server using the provided launcher script:

```bash
python start.py
```

#### Command Line Options

```bash
# Run with default configuration
python start.py

# Run in verbose mode (debug logs)
python start.py -v

# Run with a specific configuration profile (see config_examples.py)
python start.py -c living_room

# Run in web-only mode (no local playback, just stream relay)
python start.py --web-only
```

### Music Assistant Integration

The server is implemented in a Music Assistant plugin that integrates AriaCast as an audio source:

[Link to plug in](https://github.com/AirPlr/AriaCast-Receiver-MusicAssistant)


## 🌐 Web Interface

Access the modern web player at:
`http://<server-ip>:8090/`

### Web Player Features
- **🎨 Dynamic Backgrounds**: Album artwork automatically becomes the blurred page background
- **📊 Real-time Metadata**: Title, artist, album, and playback progress
- **🎵 Spectrum Visualizer**: Colorful audio frequency analysis
- **🎛️ Volume Control**: Styled slider with smooth animations
- **📈 Live Stats**: Buffer status, sample rate, channels, and connection state
- **📱 Responsive**: Works perfectly on all device sizes

## 📡 AriaCast Protocol Specification

### Architecture

AriaCast uses a **client-server** architecture where:
- **Client**: Streams audio to the server, sends metadata, and controls playback
- **Server**: Receives audio, provides playback, and exposes control/metadata endpoints

### WebSocket Endpoints

| Endpoint | Type | Direction | Description |
|----------|------|-----------|-------------|
| `/audio` | Binary | Client → Server | PCM audio stream |
| `/stats` | JSON | Server → Client | Playback statistics |
| `/control` | JSON | Bidirectional | Volume and playback control |
| `/metadata` | JSON | Bidirectional | Track metadata (title, artist, artwork) |
| `/artwork` | Binary | Server → Client | Downloaded album artwork |

### Audio Streaming (`/audio` endpoint)

Audio data is sent as **binary WebSocket frames**:

| Component | Details |
|---|---|
| **Frame Type** | Binary (OpCode 0x2) |
| **Frame Size** | Exactly 3840 bytes |
| **Content** | Raw PCM audio data |
| **Timing** | Frames should be sent at 50 frames/second (20ms intervals) |

**Audio Data Format**:
```
Sample Rate:     48000 Hz
Bit Depth:       16-bit signed integer (little-endian)
Channels:        2 (Stereo)
Duration:        20 milliseconds
Frame Size:      960 samples × 2 channels × 2 bytes = 3840 bytes
```

### Metadata (`/metadata` endpoint)

Enhanced metadata support with artwork downloading:

**Update Metadata** (Client → Server):
```json
{
  "type": "update",
  "data": {
    "title": "Song Title",
    "artist": "Artist Name",
    "album": "Album Name",
    "artwork_url": "https://example.com/cover.jpg",
    "artworkUrl": "https://example.com/cover.jpg",
    "duration_ms": 240000,
    "position_ms": 45000,
    "is_playing": true
  }
}
```

### Control (`/control` endpoint)

**Volume Commands** (Client → Server):
```json
{"command": "volume", "direction": "up"}
{"command": "volume", "direction": "down"}
{"command": "volume_set", "level": 75}
```

**Playback Commands** (Server → Client, Music Assistant integration):
```json
{"action": "play"}
{"action": "pause"}
{"action": "next"}
{"action": "previous"}
```

## 🔧 Configuration

### Server Configuration

Edit `config_examples.py` or modify the `ServerConfig` class:

```python
@dataclass
class ServerConfig:
    SERVER_NAME: str = "My AriaCast Speaker"
    VERSION: str = "1.0"
    PLATFORM: str = "Music Assistant"
    DISCOVERY_PORT: int = 12888
    STREAMING_PORT: int = 12889
    WEB_PORT: int = 8090
    ENABLE_LOCAL_AUDIO: bool = True
    AUDIO: AudioConfig = AudioConfig()
```

### Music Assistant Plugin Configuration

When setting up the AriaCast provider in Music Assistant:

- **Connected Music Assistant Player**: Choose target player or set to "Auto"
- **Allow manual player switching**: Enable to select AriaCast on any player
- **Server Name**: Display name for device discovery
- **Streaming Port**: WebSocket port (default: 12889)
- **Discovery Port**: UDP discovery port (default: 12888)

## 📋 Requirements

### Core Dependencies
- `aiohttp` - Async Web Server
- `sounddevice` - Audio Playback
- `zeroconf` - mDNS Discovery
- `numpy` - Audio Buffer Management

### Optional Dependencies
- `pycaw` - Windows Core Audio Library (volume control)
- `music-assistant` - For plugin integration

### System Requirements
- **Python**: 3.9+
- **Audio**: PortAudio-compatible sound system
- **Network**: Multicast DNS support (Bonjour/Avahi)

## 🏗️ Architecture

```
Local Area Network
├─ AriaCast Server
│  ├─ Audio Playback (sounddevice)
│  ├─ WebSocket Server (aiohttp)
│  │  ├─ /audio - Audio stream
│  │  ├─ /control - Bidirectional control
│  │  ├─ /metadata - Track information
│  │  ├─ /stats - Playback statistics
│  │  └─ /artwork - Album artwork
│  ├─ UDP Discovery (12888)
│  ├─ mDNS Advertiser (_audiocast._tcp)
│  └─ Web Dashboard (8090)
│
├─ Music Assistant (Optional)
│  └─ AriaCast Provider Plugin
│
└─ AriaCast Clients
   ├─ Discovery Phase (mDNS/UDP)
   └─ Streaming Phase (WebSockets)
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit pull requests, report issues, or suggest enhancements.

## 📄 License

MIT License - see LICENSE file for details

---

**Made with ❤️ for seamless audio streaming**
