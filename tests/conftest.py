"""Camera fixtures are available to tests only, never as application sources."""
import pytest

from rtsp_server import RtspCamera


@pytest.fixture
def rtsp_camera():
    with RtspCamera() as camera:
        yield camera


@pytest.fixture
def two_rtsp_cameras(rtsp_camera):
    with RtspCamera() as second:
        yield rtsp_camera, second
