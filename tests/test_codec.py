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
        vf = prepare_frame(source, i, encoder)
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
