"""
AriaCast Protocol Server
High-performance audio streaming using sounddevice for stable playback.
"""

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

import aiohttp
from aiohttp import web
from zeroconf import ServiceInfo, Zeroconf

# ============================================================================
# Optional Dependencies
# ============================================================================

try:
    import sounddevice as sd
    import numpy as np
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    np = None

try:
    import av
    AV_AVAILABLE = True
except ImportError:
    AV_AVAILABLE = False


# ============================================================================
# Logging Configuration
# ============================================================================

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
    SAMPLE_WIDTH: int = 2  # 16-bit
    FRAME_DURATION_MS: int = 20
    FRAME_SIZE: int = 3840  # 48000 * 2 * 2 * 0.020


@dataclass
class ServerConfig:
    """Server configuration parameters."""
    SERVER_NAME: str = "AudioCast Speaker"
    VERSION: str = "1.0"
    PLATFORM: str = "Windows"
    CODECS: List[str] = field(default_factory=lambda: ["PCM", "H264"])
    DISCOVERY_PORT: int = 12888
    STREAMING_PORT: int = 12889
    VIDEO_PORT: int = 12890
    WEB_PORT: int = 0  # Disabled
    HOST: str = "0.0.0.0"
    AUDIO: AudioConfig = field(default_factory=AudioConfig)
    ENABLE_LOCAL_AUDIO: bool = True
    ENABLE_LOCAL_VIDEO_WINDOW: bool = True
    ENABLE_DISCOVERY: bool = True
    VIDEO_DELAY: float = 0.5
    AUDIO_DELAY: float = 1.0  # Increased to 1s by default for sync issues


# ============================================================================
# Metadata Handler
# ============================================================================

class MetadataHandler:
    """Handles metadata storage and updates."""

    def __init__(self) -> None:
        self._metadata: Dict[str, Any] = {}

    def update(self, metadata: Dict[str, Any]) -> None:
        """Update metadata with new values."""
        self._metadata.update(metadata)

    def get(self) -> Dict[str, Any]:
        """Get current metadata copy."""
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
    Uses pycaw on Windows for native volume control, or PowerShell fallback.
    """
    
    def __init__(self) -> None:
        self.step = 2.0  # Volume change step in dB
        self._use_powershell = False
        self._init_volume_control()
    
    def _init_volume_control(self) -> None:
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
    
    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.stream = None
        self.running = False
        self.lock = threading.Lock()
        
        # Frame queue - thread-safe deque
        self.max_frames = 100  # 2 seconds buffer
        self.frame_queue = deque(maxlen=self.max_frames)
        
        # Leftover from previous callback
        self.leftover = np.array([], dtype=np.int16) if np else None
        
        # Statistics
        self.received_frames = 0
        self.played_samples = 0
        self.underruns = 0
        self.overruns = 0
    
    def write_frame(self, data: bytes) -> bool:
        """Add PCM frame to queue."""
        if not SOUNDDEVICE_AVAILABLE:
            return False

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
    
    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        """
        Sounddevice callback - called by audio hardware thread.
        outdata: numpy array to fill with samples
        frames: number of frames (samples per channel) requested
        """
        if status:
            logger.debug(f"Audio status: {status}")
            
        samples_needed = frames * self.config.CHANNELS
        
        with self.lock:
            # Start with leftover from previous callback
            collected = self.leftover.copy() if len(self.leftover) > 0 else np.array([], dtype=np.int16)
            
            # Collect frames until we have enough data
            while len(collected) < samples_needed and self.frame_queue:
                frame_samples = self.frame_queue.popleft()
                collected = np.concatenate([collected, frame_samples])
            
            if len(collected) >= samples_needed:
                # We have enough data to fill the buffer
                data_to_play = collected[:samples_needed]
                self.leftover = collected[samples_needed:]
                
                # Copy to output buffer
                outdata[:] = data_to_play.reshape(-1, self.config.CHANNELS)
                self.played_samples += frames
            else:
                # Underflow - not enough data, fill with silence
                silence = np.zeros(samples_needed - len(collected), dtype=np.int16)
                if len(collected) > 0:
                    outdata[:] = np.concatenate([collected, silence]).reshape(-1, self.config.CHANNELS)
                else:
                    outdata.fill(0)
                
                self.leftover = np.array([], dtype=np.int16)
                self.underruns += 1
                if len(collected) > 0:
                    logger.debug(f"Audio underflow: needed {samples_needed} samples, only {len(collected)} available")
    
    def open(self) -> bool:
        """Open audio output stream."""
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
    
    def start(self) -> None:
        """Start playback."""
        if self.stream:
            self.stream.start()
            logger.info("Playback started")
    
    def stop(self) -> None:
        """Stop playback."""
        if self.stream:
            self.stream.stop()
            logger.info("Playback stopped")
    
    def get_stats(self) -> Dict[str, int]:
        """Get playback statistics."""
        queued = len(self.frame_queue)
        return {
            "receivedFrames": self.received_frames,
            "playedSamples": self.played_samples,
            "underruns": self.underruns,
            "overruns": self.overruns,
            "queuedFrames": queued
        }
    
    def close(self) -> None:
        """Close and release audio stream."""
        self.running = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        logger.info("Audio closed")


# ============================================================================
# Video Decoding & Sync
# ============================================================================

class H264Decoder:
    """H.264 decoder using PyAV."""
    
    def __init__(self, show_window: bool = False) -> None:
        self.codec = None
        self.show_window = show_window
        self.window_name = "AriaCast Video"
        self._init_decoder()
        
    def _init_decoder(self) -> None:
        if not AV_AVAILABLE:
            return
        try:
            self.codec = av.CodecContext.create('h264', 'r')
        except Exception as e:
            logger.error(f"Failed to initialize H.264 decoder: {e}")

    def decode(self, data: bytes) -> List[Any]:
        """Decode H.264 NAL units into numpy frames."""
        if not self.codec:
            return []
        
        try:
            packets = self.codec.parse(data)
            frames = []
            for packet in packets:
                decoded_frames = self.codec.decode(packet)
                for frame in decoded_frames:
                    # Convert to numpy array immediately in the decoder thread
                    # to keep the heavy lifting off the main thread.
                    img = frame.to_ndarray(format='bgr24')
                    frames.append(img)
            return frames
        except Exception as e:
            logger.error(f"Decoding error: {e}")
            return []

    def _display_frame(self, img: Any) -> None:
        """Display numpy array image in a local OpenCV window."""
        try:
            import cv2
            # Ensure window is created if somehow lost
            if cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE) < 1:
                cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
                
            cv2.imshow(self.window_name, img)
            cv2.waitKey(1)
        except Exception as e:
            # Throttle error logging to avoid spam
            pass

    def close(self) -> None:
        """cleanup resources."""
        try:
            import cv2
            cv2.destroyAllWindows()
        except ImportError:
            pass


class VideoReceiverProtocol(asyncio.DatagramProtocol):
    """UDP video receiver protocol."""
    
    def __init__(self, server: 'AudioCastServer') -> None:
        self.server = server
        self.transport = None
        
    def connection_made(self, transport) -> None:
        self.transport = transport
        logger.info(f"Video UDP Receiver started on port {self.server.config.VIDEO_PORT}")
    
    def datagram_received(self, data: bytes, addr) -> None:
        """Handle received video packet via the main server pipeline."""
        # Process asynchronously to match WebSocket behavior
        asyncio.create_task(self.server.process_video_data(data))


# ============================================================================
# Discovery - mDNS
# ============================================================================

class mDNSDiscovery:
    """mDNS (Bonjour/Zeroconf) discovery handler."""
    
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.zeroconf: Optional[Zeroconf] = None
        self.service_info: Optional[ServiceInfo] = None
    
    def start(self) -> None:
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
        except Exception:
            return "127.0.0.1"
    
    def stop(self) -> None:
        """Stop mDNS advertising."""
        if self.zeroconf and self.service_info:
            try:
                self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
            except Exception:
                pass


# ============================================================================
# Discovery - UDP Broadcast
# ============================================================================

class UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP discovery protocol handler."""
    
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.transport = None
    
    def connection_made(self, transport) -> None:
        self.transport = transport
    
    def datagram_received(self, data: bytes, addr) -> None:
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
        except Exception:
            pass
    
    @staticmethod
    def _get_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


# ============================================================================
# Main Server
# ============================================================================

class AudioCastServer:
    """Main AudioCast server."""
    
    def __init__(self, config: ServerConfig = None, verbose: bool = False) -> None:
        self.config = config or ServerConfig()
        self.verbose = verbose
        self.audio_player = SoundDevicePlayer(self.config.AUDIO)
        self.volume_controller = VolumeController()
        self.mdns_discovery = mDNSDiscovery(self.config)
        self.audio_client: Optional[web.WebSocketResponse] = None
        
        # Audio/Video Sync State
        self.playback_started = False
        self.audio_start_pos = 0
        self.audio_start_sample_pos = 0
        self.audio_start_system_time = 0.0
        self.video_start_time = 0.0
        self.last_video_display_time = 0.0
        
        # Asyncio / Concurrency
        self.loop = asyncio.get_event_loop()
        from concurrent.futures import ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.stats_task: Optional[asyncio.Task] = None
        self.video_sync_task: Optional[asyncio.Task] = None
        
        # Handlers & Buffers
        self.metadata_handler = MetadataHandler()
        self.video_buffer: deque = deque(maxlen=300)
        self.video_decoder = H264Decoder(show_window=self.config.ENABLE_LOCAL_VIDEO_WINDOW)
        
        # Clients
        self.control_clients: List[web.WebSocketResponse] = []
        self.stats_clients: List[web.WebSocketResponse] = []
        self.metadata_clients: List[web.WebSocketResponse] = []
        
        # Artwork
        self._artwork_bytes = None
        self._artwork_timestamp = 0
    
    async def start(self) -> None:
        """Start server and all components."""
        logger.info("=" * 60)
        logger.info("AudioCast Server (Local Only Mode)")
        logger.info("=" * 60)
        
        if self.config.ENABLE_LOCAL_AUDIO:
            if not self.audio_player.open():
                logger.error("Failed to open audio")
                return
        
        if self.config.ENABLE_DISCOVERY:
            self.mdns_discovery.start()
        
        if self.config.ENABLE_LOCAL_VIDEO_WINDOW:
            import cv2
            try:
                cv2.namedWindow(self.video_decoder.window_name, cv2.WINDOW_NORMAL)
                cv2.waitKey(1)
                logger.debug("OpenCV window pre-initialized")
            except Exception as e:
                logger.error(f"Failed to pre-initialize OpenCV window: {e}")
            await self._start_udp_discovery()
        
        self.stats_task = asyncio.create_task(self._stats_loop())
        self.video_sync_task = asyncio.create_task(self._video_sync_loop())
        await self._start_video_receiver()
        
        # --- HTTP / WebSocket Server ---
        app = web.Application()
        app.router.add_get('/audio', self.handle_audio_ws)
        app.router.add_get('/control', self.handle_control_ws)
        app.router.add_get('/stats', self.handle_stats_ws)
        app.router.add_get('/metadata', self.handle_metadata_ws)
        app.router.add_get('/video', self.handle_video_stream_ws)

        try:
            import aiohttp_cors
            cors = aiohttp_cors.setup(app, defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                )
            })
            for route in list(app.router.routes()):
                cors.add(route)
        except ImportError:
            pass

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.HOST, self.config.STREAMING_PORT)
        await site.start()
        
        logger.info(f"Server listening on ws://{self.config.HOST}:{self.config.STREAMING_PORT}")
        logger.info("=" * 60)
        
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            await self._shutdown()
            await runner.cleanup()
    
    async def _start_udp_discovery(self) -> None:
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

    async def _start_video_receiver(self) -> None:
        """Start UDP video receiver."""
        try:
            loop = asyncio.get_event_loop()
            await loop.create_datagram_endpoint(
                lambda: VideoReceiverProtocol(self),
                local_addr=(self.config.HOST, self.config.VIDEO_PORT)
            )
            logger.info(f"Video receiver: port {self.config.VIDEO_PORT}")
        except Exception as e:
            logger.error(f"Video UDP error: {e}")
    
    async def process_video_data(self, data: bytes) -> None:
        """Process incoming video data (decode and schedule) from any source."""
        # 1. Decode in thread pool
        if self.verbose:
            logger.debug(f"Video data {len(data)} bytes")
            
        frames = await self.loop.run_in_executor(
            self.executor, 
            self.video_decoder.decode, 
            data
        )
        
        if self.verbose and frames:
            logger.debug(f"Decoded {len(frames)} frames")

        # 2. Schedule frames
        now = self.loop.time()
        for frame in frames:
            # Video delay to match audio latency
            audio_latency = self.config.VIDEO_DELAY 
            now = self.loop.time()
            
            # Ensure frames are spaced out but don't drift too far from 'now'
            min_spacing = 1.0 / 30.0
            
            # Calculate target time, limiting forward drift
            display_time = max(now + audio_latency, self.last_video_display_time + min_spacing)
            display_time = min(display_time, now + 1.0) 
            
            self.last_video_display_time = display_time
            self.video_buffer.append((display_time, frame))

    async def handle_video_stream_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for incoming video stream."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        peer = request.remote
        logger.info(f"Video stream source connected: {peer}")
        
        # Reset sync timing on new connection
        self.video_start_time = self.loop.time()
        self.last_video_display_time = self.loop.time()

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    await self.process_video_data(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f'Video stream WebSocket error: {ws.exception()}')
        finally:
            logger.info(f"Video stream source disconnected: {peer}")
            self.video_buffer.clear()
        return ws

    async def _video_sync_loop(self) -> None:
        """Loop to display video frames in sync with audio clock."""
        while True:
            try:
                # Use audio playback position as the master clock
                if self.playback_started and self.audio_start_system_time > 0:
                    audio_pos_samples = self.audio_player.played_samples
                    # Calculate seconds elapsed since playback started for THIS connection
                    audio_elapsed = (audio_pos_samples - self.audio_start_sample_pos) / self.config.AUDIO.SAMPLE_RATE
                    # Master time is start_time + elapsed_audio
                    master_now = self.audio_start_system_time + audio_elapsed
                else:
                    master_now = self.loop.time()
                
                frame_to_show = None
                skipped = 0
                
                # Pop all ready frames, keeping only the most recent one
                while self.video_buffer and self.video_buffer[0][0] <= master_now:
                    _, frame_to_show = self.video_buffer.popleft()
                    skipped += 1
                
                if frame_to_show is not None:
                    if self.verbose and skipped > 1:
                        logger.debug(f"Video sync: showing 1 frame, dropped {skipped-1} overdue")
                    
                    self.video_decoder._display_frame(frame_to_show)
                    
                    if skipped > 3 and self.verbose:
                        logger.debug(f"Video sync: jumped {skipped-1} frames to catch up")
                
                import cv2
                cv2.waitKey(1)
                await asyncio.sleep(0.005)
            except Exception as e:
                logger.error(f"Video sync loop error: {e}")
                await asyncio.sleep(1)

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
        
        try:
            await ws.send_json({
                "status": "READY",
                "sample_rate": self.config.AUDIO.SAMPLE_RATE,
                "channels": self.config.AUDIO.CHANNELS,
                "frame_size": self.config.AUDIO.FRAME_SIZE
            })
        except Exception:
            await ws.close()
            self.audio_client = None
            return ws
        
        prebuffer = int(self.config.AUDIO_DELAY / 0.020)  # Convert seconds to frames (20ms)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if self.config.ENABLE_LOCAL_AUDIO:
                        self.audio_player.write_frame(msg.data)
                    
                    if self.config.ENABLE_LOCAL_AUDIO and not self.playback_started:
                        if self.audio_player.received_frames >= prebuffer:
                            self.audio_player.start()
                            self.playback_started = True
                            
                            # Baseline the clocks
                            self.audio_start_system_time = self.loop.time()
                            self.audio_start_sample_pos = self.audio_player.played_samples
                            
                            logger.info(f"Playback started ({prebuffer} frames buffered)")
                            if hasattr(self.audio_player.stream, 'latency'):
                                lat = self.audio_player.stream.latency
                                logger.info(f"Hardware audio latency: {lat*1000:.1f}ms")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception as e:
            logger.error(f"Audio error: {e}")
        finally:
            if self.config.ENABLE_LOCAL_AUDIO:
                self.audio_player.stop()
            self.playback_started = False
            self.audio_client = None
            logger.info(f"Audio disconnected: {peer}")
        return ws

    async def handle_control_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /control endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.control_clients.append(ws)
        try:
            await ws.send_json({
                "status": "READY",
                "volume_available": self.volume_controller.available,
                "current_volume": self.volume_controller.get_volume()
            })
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        response = await self._handle_command(data)
                        await ws.send_json(response)
                    except Exception:
                        pass
        finally:
            if ws in self.control_clients:
                self.control_clients.remove(ws)
        return ws

    async def handle_stats_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /stats endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.stats_clients.append(ws)
        try:
            async for msg in ws: pass
        finally:
            if ws in self.stats_clients:
                self.stats_clients.remove(ws)
        return ws

    async def handle_metadata_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /metadata endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.metadata_clients.append(ws)
        try:
            await ws.send_json(self.metadata_handler.get())
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._update_metadata(data)
                    except Exception:
                        pass
        finally:
            if ws in self.metadata_clients:
                self.metadata_clients.remove(ws)
        return ws

    async def _stats_loop(self) -> None:
        """Log statistics periodically."""
        while True:
            try:
                await asyncio.sleep(0.5)
                stats = self.audio_player.get_stats()
                if self.verbose:
                    msg = f"\r[Local] Q:{stats['queuedFrames']:3d} | Rcvd:{stats['receivedFrames']:6d} | Play:{stats['playedSamples']:6d} | Under:{stats['underruns']:3d}"
                    sys.stdout.write(msg)
                    sys.stdout.flush()
                # Broadcast to stats clients
                for client in list(self.stats_clients):
                    try:
                        await client.send_json(stats)
                    except Exception:
                        self.stats_clients.remove(client)
            except asyncio.CancelledError:
                break

    async def _shutdown(self) -> None:
        """Cleanup resources."""
        logger.info("\nShutting down...")
        if self.stats_task: self.stats_task.cancel()
        if self.video_sync_task: self.video_sync_task.cancel()
        if self.executor: self.executor.shutdown()
        if self.config.ENABLE_LOCAL_AUDIO: self.audio_player.close()
        self.mdns_discovery.stop()
        try:
            import cv2
            cv2.destroyAllWindows()
        except Exception:
            pass

    async def _update_metadata(self, metadata: dict) -> None:
        self.metadata_handler.update(metadata)
        if self.verbose:
            logger.info(f"Now playing: {metadata.get('artist')} - {metadata.get('title')}")
        for client in list(self.metadata_clients):
            try:
                await client.send_json(metadata)
            except Exception:
                self.metadata_clients.remove(client)

    async def _handle_command(self, data: dict) -> dict:
        command = data.get("command")
        if command == "volume_up":
            level = self.volume_controller.volume_up()
            return {"level": level}
        if command == "volume_down":
            level = self.volume_controller.volume_down()
            return {"level": level}
        if command == "set_volume":
            level = data.get("level", 50)
            self.volume_controller.set_volume(level)
            return {"level": level}
        return {"status": "UNKNOWN"}


async def run_server(config: ServerConfig = None, verbose: bool = False) -> None:
    server = AudioCastServer(config, verbose=verbose)
    await server.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="AriaCast Server")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host address to bind to")
    parser.add_argument("--port", type=int, default=12889, help="Streaming port (default: 12889)")
    parser.add_argument("--name", type=str, default="AudioCast Speaker", help="Server name for discovery")
    parser.add_argument("--no-audio", action="store_true", help="Disable local audio playback")
    parser.add_argument("--discover", action="store_true", help="Enable mDNS discovery")
    parser.add_argument("--video-delay", type=float, default=0.5, help="Video delay in seconds for synchronization (default: 0.5)")
    parser.add_argument("--audio-delay", type=float, default=1.0, help="Audio delay (buffer) in seconds (default: 1.0)")
    
    args = parser.parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        
    config = ServerConfig(
        SERVER_NAME=args.name,
        STREAMING_PORT=args.port,
        HOST=args.host,
        ENABLE_LOCAL_AUDIO=not args.no_audio,
        ENABLE_DISCOVERY=args.discover,
        VIDEO_DELAY=args.video_delay,
        AUDIO_DELAY=args.audio_delay
    )
    
    try:
        asyncio.run(run_server(config, verbose=args.verbose))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
