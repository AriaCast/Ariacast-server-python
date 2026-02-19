import av
try:
    from PIL import Image
    print("PIL: OK")
except ImportError:
    print("PIL: MISSING")

try:
    codec = av.CodecContext.create('h264', 'r')
    print("AV H264: OK")
except Exception as e:
    print(f"AV H264 error: {e}")
