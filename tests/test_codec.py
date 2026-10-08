import av
import numpy as np

from video_demo.media import make_encoder, make_decoder, prepare_frame
from video_demo.protocol import Assembler, Meta, Packetizer, START, nals, parse_packet


def test_real_h264_roundtrip_preserves_pts_and_rgb():
    encoder = make_encoder('libx264', 320, 180, 30, 1_000_000)
    decoder, _ = make_decoder('software')
    packetizer = Packetizer(mtu=400)
    source = np.zeros((180, 320, 3), dtype=np.uint8)
    source[:] = (200, 30, 50)
    for i in range(4):
        vf = prepare_frame(av.VideoFrame.from_ndarray(source, format='rgb24'), i, encoder)
        if i == 3:
            vf.pict_type = av.video.frame.PictureType.I
        packets = encoder.encode(vf)
        assert len(packets) == 1
        raw = bytes(packets[0])
        if i in (0, 3):
            assert any(n[0] & 31 == 5 for n in nals(raw))
        assembler = Assembler()
        completed = None
        for data in reversed(packetizer.packetize(raw, Meta(0, 5, i, 100 + i))):
            completed = assembler.add(parse_packet(data), 1000) or completed
        assert completed is not None
        packet = av.Packet(completed.bitstream)
        packet.pts = packet.dts = i
        frames = decoder.decode(packet)
        assert frames[0].pts == i
        decoded = frames[0].reformat(format='rgb24', src_colorspace='ITU709').to_ndarray()
        assert np.abs(decoded.astype(float) - source).mean() < 5


def test_decoded_capture_frames_marked_intra_do_not_make_every_frame_idr(tmp_path):
    # FFmpeg's rawvideo decoder (AVFoundation/DirectShow/V4L2 capture) and MJPEG
    # mark every frame as I; reformat copies that picture type to the encoder input.
    width, height, count = 64, 48, 12
    raw = tmp_path / 'camera.nv12'
    rng = np.random.default_rng(3)
    raw.write_bytes(rng.integers(0, 256, width * height * 3 // 2 * count, dtype=np.uint8).tobytes())
    encoder = make_encoder('libx264', width, height, 30, 300_000)
    idr = []
    with av.open(str(raw), format='rawvideo',
                 options={'video_size': f'{width}x{height}', 'pixel_format': 'nv12', 'framerate': '30'}) as source:
        for pts, frame in enumerate(source.decode(video=0)):
            assert frame.pict_type == av.video.frame.PictureType.I
            prepared = prepare_frame(frame, pts, encoder)
            if pts in (0, 8):  # sender.encode(): first frame and a keyframe request
                prepared.pict_type = av.video.frame.PictureType.I
            idr += [any(n[0] & 31 == 5 for n in nals(bytes(p))) for p in encoder.encode(prepared)]
    assert len(idr) == count
    assert [i for i, key in enumerate(idr) if key] == [0, 8]
