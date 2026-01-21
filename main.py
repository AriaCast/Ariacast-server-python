"""
AriaCast Protocol Server
High-performance audio streaming using sounddevice for stable playback.
"""

import asyncio
import json
import logging
import socket
import struct
import subprocess
import sys
from collections import deque
from typing import Dict, List, Any
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from zeroconf import ServiceInfo, Zeroconf

try:
    import sounddevice as sd
    import numpy as np
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class AudioConfig:
    """Audio stream configuration parameters."""
    SAMPLE_RATE: int = 48000
    CHANNELS: int = 2
    SAMPLE_WIDTH: int = 2  # 16-bit = 2 bytes
    FRAME_DURATION_MS: int = 20
    FRAME_SIZE: int = 3840  # 48000 * 2 channels * 2 bytes * 0.020s


@dataclass
class ServerConfig:
    """Server configuration parameters."""
    SERVER_NAME: str = "AudioCast Speaker"
    VERSION: str = "1.0"
    PLATFORM: str = "Windows"
    CODECS: List[str] = None
    DISCOVERY_PORT: int = 12888
    STREAMING_PORT: int = 12889
    WEB_PORT: int = 8090
    HOST: str = "0.0.0.0"
    AUDIO: AudioConfig = None
    ENABLE_LOCAL_AUDIO: bool = True
    
    def __post_init__(self):
        if self.CODECS is None:
            self.CODECS = ["PCM"]
        if self.AUDIO is None:
            self.AUDIO = AudioConfig()


# ============================================================================
# Metadata Handler
# ============================================================================

class MetadataHandler:
    """Handles metadata storage and updates."""

    def __init__(self):
        self._metadata: Dict[str, Any] = {}

    def update(self, metadata: Dict[str, Any]) -> None:
        """Update metadata with new values."""
        self._metadata.update(metadata)

    def get(self) -> Dict[str, Any]:
        """Get current metadata."""
        return self._metadata.copy()

    def clear(self) -> None:
        """Clear all metadata."""
        self._metadata.clear()


# ============================================================================
# Volume Controller
# ============================================================================

class VolumeController:
    """
    System volume controller.
    Uses pycaw on Windows for native volume control.
    """
    
    def __init__(self):
        self.step = 2.0  # Volume change step in dB
        self._use_powershell = False
        self._init_volume_control()
    
    def _init_volume_control(self):
        """Initialize platform-specific volume control."""
        self.available = False
        self.volume = None
        self.min_db = -65.25
        self.max_db = 0.0
        
        if sys.platform == 'win32':
            try:
                from pycaw.pycaw import AudioUtilities
                
                device = AudioUtilities.GetSpeakers()
                self.volume = device.EndpointVolume
                
                # Get volume range
                vol_range = self.volume.GetVolumeRange()
                self.min_db = vol_range[0]
                self.max_db = vol_range[1]
                
                self.available = True
                logger.info(f"Volume control: pycaw (range: {self.min_db:.1f}dB to {self.max_db:.1f}dB)")
            except ImportError:
                logger.warning("pycaw not installed - pip install pycaw")
                self._use_powershell = True
                self.available = True
            except Exception as e:
                logger.warning(f"pycaw failed: {e}")
                self._use_powershell = True
                self.available = True
        else:
            logger.warning(f"Volume control not implemented for {sys.platform}")
    
    def _powershell_volume(self, action: str) -> int:
        """Control volume via PowerShell and SendKeys."""
        try:
            if action == "up":
                script = '''
                Add-Type -AssemblyName System.Windows.Forms
                [System.Windows.Forms.SendKeys]::SendWait([char]0xAF)
                '''
                subprocess.run(["powershell", "-Command", script], 
                             capture_output=True, timeout=2)
                logger.info("Volume up (PowerShell)")
                return 1
            elif action == "down":
                script = '''
                Add-Type -AssemblyName System.Windows.Forms
                [System.Windows.Forms.SendKeys]::SendWait([char]0xAE)
                '''
                subprocess.run(["powershell", "-Command", script], 
                             capture_output=True, timeout=2)
                logger.info("Volume down (PowerShell)")
                return 1
        except Exception as e:
            logger.error(f"PowerShell volume error: {e}")
        return -1
    
    def get_volume(self) -> int:
        """Get current volume level (0-100)."""
        if not self.available:
            return -1
        if self._use_powershell:
            return 50
        try:
            # Use scalar (0.0 - 1.0) and convert to percentage
            level = self.volume.GetMasterVolumeLevelScalar()
            return int(level * 100)
        except Exception as e:
            logger.error(f"Get volume error: {e}")
            return -1
    
    def set_volume(self, level: int) -> bool:
        """Set volume level (0-100)."""
        if not self.available:
            return False
        if self._use_powershell:
            return False
        try:
            level = max(0, min(100, level))
            self.volume.SetMasterVolumeLevelScalar(level / 100.0, None)
            return True
        except Exception as e:
            logger.error(f"Set volume error: {e}")
            return False
    
    def volume_up(self) -> int:
        """Increase volume by step. Returns new level."""
        if self._use_powershell:
            return self._powershell_volume("up")
        try:
            current_db = self.volume.GetMasterVolumeLevel()
            new_db = min(self.max_db, current_db + self.step)
            self.volume.SetMasterVolumeLevel(new_db, None)
            new_level = self.get_volume()
            logger.info(f"Volume up: {current_db:.1f}dB → {new_db:.1f}dB ({new_level}%)")
            return new_level
        except Exception as e:
            logger.error(f"Volume up error: {e}")
            return -1
    
    def volume_down(self) -> int:
        """Decrease volume by step. Returns new level."""
        if self._use_powershell:
            return self._powershell_volume("down")
        try:
            current_db = self.volume.GetMasterVolumeLevel()
            new_db = max(self.min_db, current_db - self.step)
            self.volume.SetMasterVolumeLevel(new_db, None)
            new_level = self.get_volume()
            logger.info(f"Volume down: {current_db:.1f}dB → {new_db:.1f}dB ({new_level}%)")
            return new_level
        except Exception as e:
            logger.error(f"Volume down error: {e}")
            return -1


# ============================================================================
# SoundDevice Audio Player
# ============================================================================

class SoundDevicePlayer:
    """
    Audio player using sounddevice library.
    Uses OutputStream with callback for stable, hardware-timed playback.
    """
    
    def __init__(self, config: AudioConfig):
        self.config = config
        self.stream = None
        self.running = False
        
        # Frame queue - thread-safe deque
        self.max_frames = 100  # 2 seconds buffer
        self.frame_queue = deque(maxlen=self.max_frames)
        
        # Leftover from previous callback
        self.leftover = np.array([], dtype=np.int16)
        
        # Statistics
        self.received_frames = 0
        self.played_callbacks = 0
        self.underruns = 0
        self.overruns = 0
    
    def write_frame(self, data: bytes) -> bool:
        """Add frame to queue."""
        if len(data) != self.config.FRAME_SIZE:
            logger.warning(f"Wrong frame size: {len(data)} (expected {self.config.FRAME_SIZE})")
            return False
        
        self.received_frames += 1
        
        if len(self.frame_queue) >= self.max_frames:
            self.overruns += 1
        
        # Convert bytes to numpy array - LITTLE ENDIAN (standard PCM)
        samples = np.frombuffer(data, dtype='<i2')
        self.frame_queue.append(samples)
        return True
    
    def _audio_callback(self, outdata, frames, time_info, status):
        """
        Sounddevice callback - called by audio hardware.
        outdata: numpy array to fill with samples
        frames: number of frames (samples per channel) requested
        """
        if status:
            logger.warning(f"Audio status: {status}")
        
        samples_needed = frames * self.config.CHANNELS
        
        # Start with leftover
        collected = self.leftover.copy() if len(self.leftover) > 0 else np.array([], dtype=np.int16)
        
        # Collect frames until we have enough
        while len(collected) < samples_needed:
            try:
                frame_samples = self.frame_queue.popleft()
                collected = np.concatenate([collected, frame_samples])
            except IndexError:
                # Queue empty - fill with silence
                silence = np.zeros(samples_needed - len(collected), dtype=np.int16)
                collected = np.concatenate([collected, silence])
                self.underruns += 1
                break
        
        # Save leftover
        if len(collected) > samples_needed:
            self.leftover = collected[samples_needed:]
            collected = collected[:samples_needed]
        else:
            self.leftover = np.array([], dtype=np.int16)
        
        # Reshape to (frames, channels) and copy to output
        outdata[:] = collected.reshape(-1, self.config.CHANNELS)
        self.played_callbacks += 1
    
    def open(self) -> bool:
        """Open audio stream."""
        if not SOUNDDEVICE_AVAILABLE:
            logger.error("sounddevice not available - pip install sounddevice")
            return False
        
        try:
            # Query default device
            device_info = sd.query_devices(kind='output')
            logger.info(f"Audio device: {device_info['name']}")
            
            # blocksize = samples per callback (per channel)
            # 960 samples = 20ms @ 48kHz
            blocksize = self.config.SAMPLE_RATE // 50
            
            self.stream = sd.OutputStream(
                samplerate=self.config.SAMPLE_RATE,
                channels=self.config.CHANNELS,
                dtype=np.int16,
                blocksize=blocksize,
                callback=self._audio_callback,
                latency='low'
            )
            
            self.running = True
            logger.info(f"Audio: {self.config.CHANNELS}ch, {self.config.SAMPLE_RATE}Hz, 16-bit")
            logger.info(f"Buffer: {self.max_frames} frames ({self.max_frames * 20}ms)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to open audio: {e}")
            return False
    
    def start(self):
        """Start playback."""
        if self.stream:
            self.stream.start()
            logger.info("Playback started")
    
    def stop(self):
        """Stop playback."""
        if self.stream:
            self.stream.stop()
            logger.info("Playback stopped")
    
    def get_stats(self) -> Dict:
        """Get statistics."""
        queued = len(self.frame_queue)
        buffer_pct = queued / self.max_frames * 100
        
        return {
            "receivedFrames": self.received_frames,
            "playedCallbacks": self.played_callbacks,
            "underruns": self.underruns,
            "overruns": self.overruns,
            "queuedFrames": queued,
            "bufferLevel": f"{buffer_pct:.1f}%"
        }
    
    def close(self):
        """Close stream."""
        self.running = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except:
                pass
        logger.info("Audio closed")


# ============================================================================
# Discovery - mDNS
# ============================================================================

class mDNSDiscovery:
    """mDNS (Bonjour/Zeroconf) discovery handler."""
    
    def __init__(self, config: ServerConfig):
        self.config = config
        self.zeroconf = None
        self.service_info = None
    
    def start(self):
        """Start mDNS advertising."""
        try:
            self.zeroconf = Zeroconf()
            local_ip = self._get_local_ip()
            
            self.service_info = ServiceInfo(
                "_audiocast._tcp.local.",
                f"{self.config.SERVER_NAME}._audiocast._tcp.local.",
                addresses=[socket.inet_aton(local_ip)],
                port=self.config.STREAMING_PORT,
                properties={
                    "version": self.config.VERSION,
                    "samplerate": str(self.config.AUDIO.SAMPLE_RATE),
                    "channels": str(self.config.AUDIO.CHANNELS),
                },
            )
            self.zeroconf.register_service(self.service_info)
            logger.info(f"mDNS: {self.config.SERVER_NAME}")
        except Exception as e:
            logger.error(f"mDNS error: {e}")
    
    @staticmethod
    def _get_local_ip() -> str:
        """Get local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def stop(self):
        """Stop mDNS advertising."""
        if self.zeroconf and self.service_info:
            try:
                self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
            except:
                pass


# ============================================================================
# Discovery - UDP Broadcast
# ============================================================================

class UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP discovery protocol handler."""
    
    def __init__(self, config: ServerConfig):
        self.config = config
        self.transport = None
    
    def connection_made(self, transport):
        self.transport = transport
    
    def datagram_received(self, data: bytes, addr):
        """Handle discovery request."""
        try:
            if data.decode('utf-8').strip() == "DISCOVER_AUDIOCAST":
                local_ip = self._get_local_ip()
                response = {
                    "server_name": self.config.SERVER_NAME,
                    "ip": local_ip,
                    "port": self.config.STREAMING_PORT,
                    "samplerate": self.config.AUDIO.SAMPLE_RATE,
                    "channels": self.config.AUDIO.CHANNELS,
                }
                self.transport.sendto(json.dumps(response).encode(), addr)
        except:
            pass
    
    @staticmethod
    def _get_local_ip() -> str:
        """Get local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"


# ============================================================================
# Main Server
# ============================================================================

class AudioCastServer:
    """Main AudioCast server."""
    
    def __init__(self, config: ServerConfig = None, verbose: bool = False):
        self.config = config or ServerConfig()
        self.verbose = verbose
        self.audio_player = SoundDevicePlayer(self.config.AUDIO)
        self.volume_controller = VolumeController()
        self.mdns_discovery = mDNSDiscovery(self.config)
        self.stats_clients: List[web.WebSocketResponse] = []
        self.metadata_clients: List[web.WebSocketResponse] = []
        self.listening_clients: List[web.WebSocketResponse] = []  # Web/HTML listeners
        self.http_queues: List[asyncio.Queue] = [] # HTTP stream listeners (VLC, etc)
        self.audio_client = None
        self.stats_task = None
        self.playback_started = False
        
        # Metadata handler
        self.metadata_handler = MetadataHandler()
        
        # Control clients (controllers and devices connect here)
        self.control_clients: List[web.WebSocketResponse] = []
        
        # Artwork storage
        self._artwork_bytes: bytes = b""
        self._artwork_timestamp: int = 0
    
    async def start(self):
        """Start server and all components."""
        logger.info("=" * 60)
        logger.info("AudioCast Server")
        logger.info("=" * 60)
        
        if self.config.ENABLE_LOCAL_AUDIO:
            if not self.audio_player.open():
                logger.error("Failed to open audio")
                return
        else:
            logger.info("Local audio playback disabled (Web-only mode)")
        
        self.mdns_discovery.start()
        await self._start_udp_discovery()
        
        self.stats_task = asyncio.create_task(self._stats_loop())
        
        # --- Stream/Input Server ---
        app_stream = web.Application()
        app_stream.router.add_get('/audio', self.handle_audio_ws)
        app_stream.router.add_get('/control', self.handle_control_ws)
        app_stream.router.add_get('/stats', self.handle_stats_ws)
        app_stream.router.add_get('/metadata', self.handle_metadata_ws)
        app_stream.router.add_post('/metadata', self.handle_metadata_api)
        app_stream.router.add_get('/artwork', self.handle_artwork)

        # --- Web/Player Server ---
        app_web = web.Application()
        app_web.router.add_get('/', self.handle_index)
        app_web.router.add_get('/listen', self.handle_listen_ws)
        app_web.router.add_get('/stream.wav', self.handle_stream_wav)
        app_web.router.add_get('/metadata', self.handle_metadata_ws)
        app_web.router.add_get('/api/metadata', self.handle_metadata_api)
        app_web.router.add_get('/artwork', self.handle_artwork)
        
        try:
            import aiohttp_cors
            # CORS for web app
            cors_web = aiohttp_cors.setup(app_web, defaults={
            "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                )
            })
            for route in list(app_web.router.routes()):
                cors_web.add(route)
            
            # CORS for stream app (needed for control WebSocket)
            cors_stream = aiohttp_cors.setup(app_stream, defaults={
            "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                )
            })
            for route in list(app_stream.router.routes()):
                cors_stream.add(route)
        except ImportError:
            logger.warning("aiohttp_cors not installed - CORS headers not set")

        # Runner 1: Stream
        runner_stream = web.AppRunner(app_stream)
        await runner_stream.setup()
        site_stream = web.TCPSite(runner_stream, self.config.HOST, self.config.STREAMING_PORT)
        await site_stream.start()
        
        # Runner 2: Web
        runner_web = web.AppRunner(app_web)
        await runner_web.setup()
        site_web = web.TCPSite(runner_web, self.config.HOST, self.config.WEB_PORT)
        await site_web.start()
        
        logger.info(f"Stream Input: ws://*:{self.config.STREAMING_PORT}")
        logger.info(f"Web Player:   http://*:{self.config.WEB_PORT}")
        logger.info("=" * 60)
        logger.info("Ready")
        logger.info("=" * 60)
        
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            await self._shutdown(runner_stream, runner_web)
    
    async def _start_udp_discovery(self):
        """Start UDP discovery server."""
        try:
            loop = asyncio.get_event_loop()
            await loop.create_datagram_endpoint(
                lambda: UDPDiscoveryProtocol(self.config),
                local_addr=(self.config.HOST, self.config.DISCOVERY_PORT)
            )
            logger.info(f"UDP discovery: port {self.config.DISCOVERY_PORT}")
        except Exception as e:
            logger.error(f"UDP error: {e}")

    async def handle_index(self, request: web.Request) -> web.FileResponse:
        """Serve the HTML player."""
        return web.FileResponse('index.html')

    async def handle_metadata_api(self, request: web.Request) -> web.Response:
        """HTTP API handler for current metadata (GET) or updates (POST)."""
        if request.method == 'POST':
            try:
                data = await request.json()
                # Support both direct object and "data" wrapper
                metadata = data.get("data", data)
                await self._update_metadata(metadata)
                return web.json_response({"success": True})
            except Exception as e:
                return web.json_response({"success": False, "error": str(e)}, status=400)
        else:
            return web.json_response(self.metadata_handler.get())

    async def handle_artwork(self, request: web.Request) -> web.Response:
        """Serve downloaded artwork image."""
        if self._artwork_bytes:
            # Try to determine content type from bytes
            content_type = "image/jpeg"  # default
            if self._artwork_bytes.startswith(b'\xff\xd8'):
                content_type = "image/jpeg"
            elif self._artwork_bytes.startswith(b'\x89PNG'):
                content_type = "image/png"
            elif self._artwork_bytes.startswith(b'GIF8'):
                content_type = "image/gif"
            elif self._artwork_bytes.startswith(b'RIFF') and self._artwork_bytes[8:12] == b'WEBP':
                content_type = "image/webp"
            
            return web.Response(
                body=self._artwork_bytes,
                content_type=content_type,
                headers={'Cache-Control': f'max-age={3600}'}  # Cache for 1 hour
            )
        else:
            return web.Response(status=404, text="No artwork available")

    async def handle_stream_wav(self, request: web.Request) -> web.StreamResponse:
        """Handler for HTTP audio stream (VLC/Players)."""
        logger.info(f"HTTP Listener connected: {request.remote}")
        
        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': 'audio/wav',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        })
        await resp.prepare(request)
        
        # Create queue for this client
        queue = asyncio.Queue(maxsize=100) # Buffer similar to main audio
        self.http_queues.append(queue)
        
        try:
            # Write WAV Header
            # Total size: 0xFFFFFFFF (unknown/streaming)
            # Format: 1 (PCM), Channels, SampleRate, ByteRate, BlockAlign, BitsPerSample
            # Data Chunk Size: 0xFFFFFFFF
            headers = self._create_wav_header()
            await resp.write(headers)
            
            while True:
                data = await queue.get()
                await resp.write(data)
                
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            # Client disconnected
            pass
        finally:
            if queue in self.http_queues:
                self.http_queues.remove(queue)
            logger.info(f"HTTP Listener disconnected: {request.remote}")
            return resp

    def _create_wav_header(self) -> bytes:
        """Create WAV file header for streaming."""
        audio = self.config.AUDIO
        # Canonical WAV header
        # references: http://soundfile.sapp.org/doc/WaveFormat/
        
        file_size = 0xFFFFFFFF # Unknown/Max
        fmt_chunk_size = 16 # PCM
        audio_format = 1 # PCM
        channels = audio.CHANNELS
        sample_rate = audio.SAMPLE_RATE
        byte_rate = sample_rate * channels * audio.SAMPLE_WIDTH
        block_align = channels * audio.SAMPLE_WIDTH
        bits_per_sample = audio.SAMPLE_WIDTH * 8
        data_size = 0xFFFFFFFF # Unknown
        
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', file_size, b'WAVE',
            b'fmt ', fmt_chunk_size, audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample,
            b'data', data_size
        )

    async def handle_listen_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for web listeners (HTML player)."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        peer = request.remote
        logger.info(f"Web listener connected: {peer}")
        self.listening_clients.append(ws)
        
        try:
            # Send configuration
            await ws.send_json({
                "sample_rate": self.config.AUDIO.SAMPLE_RATE,
                "channels": self.config.AUDIO.CHANNELS
            })
            
            async for msg in ws:
                pass # Keep connection open
                
        except Exception as e:
            logger.error(f"Listener error: {e}")
        finally:
            if ws in self.listening_clients:
                self.listening_clients.remove(ws)
            logger.info(f"Web listener disconnected: {peer}")
        
        return ws
    
    async def handle_audio_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /audio endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        if self.audio_client is not None:
            await ws.close()
            return ws
        
        self.audio_client = ws
        self.playback_started = False
        peer = request.remote
        logger.info(f"Audio connected: {peer}")
        
        # Send handshake
        try:
            await ws.send_json({
                "status": "READY",
                "sample_rate": self.config.AUDIO.SAMPLE_RATE,
                "channels": self.config.AUDIO.CHANNELS,
                "frame_size": self.config.AUDIO.FRAME_SIZE
            })
        except:
            await ws.close()
            self.audio_client = None
            return ws
        
        prebuffer = 25  # 500ms before starting playback
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if self.config.ENABLE_LOCAL_AUDIO:
                        self.audio_player.write_frame(msg.data)
                    
                    # Broadcast to web listeners
                    for client in self.listening_clients:
                        try:
                            # Fire and forget
                            await client.send_bytes(msg.data)
                        except:
                            pass
                    
                    # Broadcast to HTTP listeners
                    for queue in self.http_queues:
                        try:
                            queue.put_nowait(msg.data)
                        except asyncio.QueueFull:
                            # Drop frame if client is too slow
                            pass

                    # Start playback after prebuffering (Local Audio)
                    if self.config.ENABLE_LOCAL_AUDIO and not self.playback_started:
                        if self.audio_player.received_frames >= prebuffer:
                            self.audio_player.start()
                            self.playback_started = True
                            logger.info(f"Playback started ({prebuffer} frames buffered)")
                            
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
                    
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if self.config.ENABLE_LOCAL_AUDIO:
                self.audio_player.stop()
            self.playback_started = False
            self.audio_client = None
            logger.info(f"Audio disconnected: {peer}")
        
        return ws
    
    async def handle_stats_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /stats endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.stats_clients.append(ws)
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except:
            pass
        finally:
            if ws in self.stats_clients:
                self.stats_clients.remove(ws)
        
        return ws
    
    async def handle_control_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /control endpoint - volume and other commands."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        peer = request.remote
        logger.info(f"Control client connected: {peer}")
        # Register this control connection (could be a controller UI or a device)
        self.control_clients.append(ws)

        # Send initial status to this client
        try:
            await ws.send_json({
                "status": "READY",
                "volume_available": self.volume_controller.available,
                "current_volume": self.volume_controller.get_volume()
            })
        except Exception:
            logger.debug("Failed to send initial control status to client")

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        logger.info("Control message received from %s: %s", peer, msg.data)
                        data = json.loads(msg.data)
                        response = await self._handle_command(data)
                        # Reply to sender
                        await ws.send_json(response)
                        # Forward the incoming command to other control clients (e.g., device)
                        await self._broadcast_control(data, exclude_ws=ws)
                    except json.JSONDecodeError:
                        await ws.send_json({"error": "Invalid JSON"})
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception as e:
            logger.error(f"Control error: {e}")
        finally:
            logger.info(f"Control client disconnected: {peer}")
            # Clean up registered control client
            if ws in self.control_clients:
                self.control_clients.remove(ws)

        return ws

    async def _broadcast_control(self, message: dict, exclude_ws: web.WebSocketResponse | None = None) -> None:
        """Send a control message to all connected control clients except exclude_ws."""
        logger.info("Broadcasting control message to control clients: %s", message)
        for client in list(self.control_clients):
            if client == exclude_ws:
                continue
            try:
                await client.send_json(message)
            except Exception as e:
                logger.debug("Failed to send control message to client: %s", e)
                if client in self.control_clients:
                    self.control_clients.remove(client)
        
    
    async def _handle_command(self, data: dict) -> dict:
        """Handle incoming control commands."""
        command = data.get("command")
        action = data.get("action")  # For Music Assistant compatibility
        
        # Handle action-based commands (from Music Assistant)
        if action:
            if action == "play":
                await self._cmd_play()
                return {"action": "play", "success": True}
            elif action == "pause":
                await self._cmd_pause()
                return {"action": "pause", "success": True}
            elif action == "next":
                await self._cmd_next()
                return {"action": "next", "success": True}
            elif action == "previous":
                await self._cmd_previous()
                return {"action": "previous", "success": True}
            elif action == "seek":
                position_ms = data.get("position_ms")
                if isinstance(position_ms, (int, float)):
                    await self._cmd_seek(int(position_ms))
                    return {"action": "seek", "position_ms": position_ms, "success": True}
                else:
                    return {"error": "position_ms must be a number"}
        
        # Handle command-based commands
        # Command-based controls (support both legacy and UI formats)
        if command == "volume":
            # Support UI format: { command: 'volume', value: 80 }
            if "value" in data and isinstance(data.get("value"), (int, float)):
                level = int(data.get("value"))
                success = self.volume_controller.set_volume(level)
                return {"command": "volume", "level": self.volume_controller.get_volume(), "success": success}

            # Legacy format: { command: 'volume', direction: 'up' }
            direction = data.get("direction")
            if direction == "up":
                new_level = self.volume_controller.volume_up()
                return {"command": "volume", "level": new_level, "success": new_level >= 0}
            elif direction == "down":
                new_level = self.volume_controller.volume_down()
                return {"command": "volume", "level": new_level, "success": new_level >= 0}
            elif direction == "get":
                level = self.volume_controller.get_volume()
                return {"command": "volume", "level": level, "success": level >= 0}
            else:
                return {"error": "Invalid volume format. Provide 'value' or 'direction'"}

        elif command == "volume_set":
            level = data.get("level")
            if isinstance(level, (int, float)):
                success = self.volume_controller.set_volume(int(level))
                return {"command": "volume_set", "level": self.volume_controller.get_volume(), "success": success}
            else:
                return {"error": "level must be a number (0-100)"}

        elif command == "play_pause":
            # Toggle playback depending on current metadata
            current_metadata = self.metadata_handler.get()
            if current_metadata.get("is_playing"):
                await self._cmd_pause()
                return {"command": "play_pause", "state": "paused", "success": True}
            else:
                await self._cmd_play()
                return {"command": "play_pause", "state": "playing", "success": True}

        elif command == "stop":
            # Map stop to pause for now
            await self._cmd_pause()
            return {"command": "stop", "success": True}

        elif command in ["play", "pause", "next", "previous"]:
            # Handle command-based playback controls
            if command == "play":
                await self._cmd_play()
            elif command == "pause":
                await self._cmd_pause()
            elif command == "next":
                await self._cmd_next()
            elif command == "previous":
                await self._cmd_previous()
            return {"command": command, "success": True}

        elif command == "seek":
            # Support UI format: { command: 'seek', value: <ms> }
            position_ms = data.get("position_ms") if data.get("position_ms") is not None else data.get("value")
            if isinstance(position_ms, (int, float)):
                await self._cmd_seek(int(position_ms))
                return {"command": "seek", "position_ms": position_ms, "success": True}
            else:
                return {"error": "position_ms must be a number"}

        else:
            return {"error": f"Unknown command: {command}"}
    
    async def _cmd_play(self) -> None:
        """Handle Play command."""
        logger.info("Received play command")
        # Update metadata to reflect playing state
        current_metadata = self.metadata_handler.get()
        if current_metadata.get("is_playing") != True:
            current_metadata["is_playing"] = True
            self.metadata_handler.update({"is_playing": True})
            await self._broadcast_metadata()
        # Send action to connected control devices
        await self._broadcast_control({"action": "play"})

    async def _cmd_pause(self) -> None:
        """Handle Pause command."""
        logger.info("Received pause command")
        # Update metadata to reflect paused state
        current_metadata = self.metadata_handler.get()
        if current_metadata.get("is_playing") != False:
            current_metadata["is_playing"] = False
            self.metadata_handler.update({"is_playing": False})
            await self._broadcast_metadata()
        await self._broadcast_control({"action": "pause"})

    async def _cmd_next(self) -> None:
        """Handle Next command."""
        logger.info("Received next command")
        # For standalone server, we could implement track navigation
        # For now, just log and acknowledge
        await self._broadcast_control({"action": "next"})
        pass
    
    async def _cmd_previous(self) -> None:
        """Handle Previous command."""
        logger.info("Received previous command")
        # For standalone server, we could implement track navigation
        # For now, just log and acknowledge
        await self._broadcast_control({"action": "previous"})
        pass
    
    async def _cmd_seek(self, position_ms: int) -> None:
        """Handle Seek command."""
        logger.info(f"Received seek command: {position_ms}ms")
        # Update metadata to reflect new position
        self.metadata_handler.update({"position_ms": position_ms})
        await self._broadcast_metadata()
        await self._broadcast_control({"action": "seek", "position_ms": position_ms})
    
    
    async def handle_metadata_ws(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket handler for /metadata endpoint.
        - Sender (audio client) pushes metadata updates
        - Subscribers receive metadata updates
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        peer = request.remote
        logger.info(f"Metadata client connected: {peer}")
        
        # Add to subscribers list
        self.metadata_clients.append(ws)
        
        # Send current metadata immediately
        try:
            await ws.send_json({
                "type": "metadata",
                "data": self.metadata_handler.get()
            })
        except:
            pass
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")
                        
                        if msg_type == "update":
                            # Client is pushing new metadata
                            metadata = data.get("data", {})
                            await self._update_metadata(metadata)
                            await ws.send_json({"type": "ack", "success": True})
                            
                        elif msg_type == "get":
                            # Client requests current metadata
                            await ws.send_json({
                                "type": "metadata",
                                "data": self.metadata_handler.get()
                            })
                            
                        elif msg_type == "clear":
                            # Clear metadata (playback stopped)
                            await self._clear_metadata()
                            await ws.send_json({"type": "ack", "success": True})
                            
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                        
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
                    
        except Exception as e:
            logger.error(f"Metadata error: {e}")
        finally:
            if ws in self.metadata_clients:
                self.metadata_clients.remove(ws)
            logger.info(f"Metadata client disconnected: {peer}")
        
        return ws
    
    async def _update_metadata(self, metadata: dict):
        """Update current metadata and broadcast to all subscribers."""
        # Track if title/artist changed for logging
        old_title = self.metadata_handler.get().get("title")
        old_artist = self.metadata_handler.get().get("artist")
        
        # Update metadata
        self.metadata_handler.update(metadata)
        
        # Handle artwork download
        artwork_url = metadata.get("artwork_url") or metadata.get("artworkUrl")
        if artwork_url and isinstance(artwork_url, str) and artwork_url.startswith("http"):
            asyncio.create_task(self._download_artwork(artwork_url))
        
        # Only log if title or artist changed
        new_title = self.metadata_handler.get().get("title")
        new_artist = self.metadata_handler.get().get("artist")
        if self.verbose and (new_title != old_title or new_artist != old_artist):
            title = new_title or "Unknown"
            artist = new_artist or "Unknown"
            logger.info(f"Now playing: {artist} - {title}")
        
        # Broadcast to all subscribers
        await self._broadcast_metadata()
    
    async def _clear_metadata(self):
        """Clear all metadata."""
        self.metadata_handler.clear()
        await self._broadcast_metadata()
    
    async def _broadcast_metadata(self):
        """Send current metadata to all subscribers."""
        message = {
            "type": "metadata",
            "data": self.metadata_handler.get()
        }
        for client in self.metadata_clients[:]:
            try:
                await client.send_json(message)
            except:
                self.metadata_clients.remove(client)
    
    async def _download_artwork(self, artwork_url: str) -> None:
        """Download artwork from URL and store it."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(artwork_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        self._artwork_bytes = await response.read()
                        self._artwork_timestamp = int(asyncio.get_event_loop().time() * 1000)
                        logger.debug(f"Downloaded artwork: {len(self._artwork_bytes)} bytes")
                        # Update metadata to point to local artwork
                        current_metadata = self.metadata_handler.get()
                        if "artwork_url" in current_metadata or "artworkUrl" in current_metadata:
                            current_metadata["artwork_url"] = "/artwork"
                            self.metadata_handler.update({"artwork_url": "/artwork"})
                            await self._broadcast_metadata()
        except Exception as e:
            logger.debug(f"Failed to download artwork: {e}")
    
    async def _stats_loop(self):
        """Send statistics to connected clients."""
        try:
            while True:
                await asyncio.sleep(1.0)
                stats = self.audio_player.get_stats()
                
                # Graphical console output (only in verbose mode)
                if self.verbose:
                    queued = stats['queuedFrames']
                    max_frames = self.audio_player.max_frames
                    bar_width = 20
                    filled = int(queued * bar_width / max_frames)
                    bar = '█' * filled + '░' * (bar_width - filled)
                    
                    sys.stdout.write(
                        f"\r[{bar}] "
                        f"Q:{queued:3d}/{max_frames} "
                        f"Rcvd:{stats['receivedFrames']:6d} "
                        f"Play:{stats['playedCallbacks']:6d} "
                        f"Under:{stats['underruns']:3d}"
                    )
                    sys.stdout.flush()
                
                # Send to WebSocket clients
                for client in self.stats_clients[:]:
                    try:
                        await client.send_json(stats)
                    except:
                        self.stats_clients.remove(client)
        except asyncio.CancelledError:
            if self.verbose:
                sys.stdout.write('\n')  # Clean line on exit
            pass
    
    async def _shutdown(self, *runners):
        """Cleanup resources."""
        logger.info("\nShutting down...")
        if self.stats_task:
            self.stats_task.cancel()
        if self.config.ENABLE_LOCAL_AUDIO:
            self.audio_player.close()
        self.mdns_discovery.stop()
        for runner in runners:
            await runner.cleanup()


# Alias for backward compatibility
AudioCastServerSD = AudioCastServer


async def run_server(config: ServerConfig = None):
    """Run the AudioCast server."""
    server = AudioCastServer(config)
    await server.start()


def main():
    """Main entry point."""
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

