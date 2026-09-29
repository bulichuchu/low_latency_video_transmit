"""Listener/Host/TLS configuration tests without starting any server or camera."""
import asyncio
import json
import ssl

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import pytest

from video_demo.cli import parser
from video_demo.webapp import create_app, normalized_hostname, web_settings


def test_listen_configuration_and_missing_tls_pair():
    args = parser().parse_args(['web', '--no-browser'])
    hosts, context, url = web_settings(args)
    assert hosts == {'localhost', '127.0.0.1', '::1'}
    assert context is None and url == 'http://127.0.0.1:8765/#/sender'
    args.bind = '0.0.0.0'
    with pytest.raises(ValueError, match='allow-host'):
        web_settings(args)
    args.allow_host = ['qnbot-macmini.qnbot.net']
    hosts, _, url = web_settings(args)
    assert 'qnbot-macmini.qnbot.net' in hosts
    assert url == 'http://qnbot-macmini.qnbot.net:8765/#/sender'
    args.tls_cert = '/not/a/certificate.pem'
    with pytest.raises(ValueError, match='together'):
        web_settings(args)
    args.tls_key = '/not/a/key.pem'
    with pytest.raises(OSError):
        web_settings(args)


@pytest.mark.parametrize('name', ['*', '*.example.com', 'http://example.com', 'example.com:8765',
                                'a/b', 'a@b', 'a#b', 'a?b', '-bad', 'bad-', 'a..b', ' ', None])
def test_reject_ambiguous_hostnames(name):
    with pytest.raises(ValueError):
        normalized_hostname(name)


def test_domain_origin_tls_and_token_checks_without_socket():
    async def exercise():
        app = create_app(allowed_hosts=['localhost', '127.0.0.1', 'qnbot-macmini.qnbot.net'])
        middleware = app.middlewares[0]
        host = 'qnbot-macmini.qnbot.net:8765'
        async def ok(request):
            return web.json_response({'ok': True})
        req = make_mocked_request('GET', '/api/bootstrap', headers={'Host': host}, app=app)
        route = await app.router.resolve(req)
        bootstrap = await middleware(req, route.handler)
        token = json.loads(bootstrap.body)['token']
        for scheme in ('http', 'https'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if scheme == 'https' else None
            req = make_mocked_request('POST', '/api/sender/stop', app=app, sslcontext=context,
                headers={'Host': host, 'Origin': f'{scheme}://{host}', 'X-Video-Token': token})
            assert (await middleware(req, ok)).status == 200
            wrong = make_mocked_request('POST', '/api/sender/stop', app=app, sslcontext=context,
                headers={'Host': host, 'Origin': 'https://untrusted.example', 'X-Video-Token': token})
            with pytest.raises(web.HTTPForbidden):
                await middleware(wrong, ok)
        for bad in ('evil.example:8765', 'qnbot-macmini.qnbot.net.attacker.com',
                    'user@qnbot-macmini.qnbot.net:8765', 'qnbot-macmini.qnbot.net:invalid'):
            with pytest.raises(web.HTTPForbidden):
                await middleware(make_mocked_request('GET', '/', headers={'Host': bad}), ok)
        with pytest.raises(web.HTTPForbidden):
            await middleware(make_mocked_request('POST', '/', headers={'Host': host}), ok)
        # Default app remains loopback-only, even if a client fabricates a host.
        with pytest.raises(web.HTTPForbidden):
            await create_app().middlewares[0](make_mocked_request('GET', '/', headers={'Host': host}), ok)
    asyncio.run(exercise())


def test_explicit_lan_launch_does_not_reuse_a_local_listener(monkeypatch):
    import video_demo.webapp as module
    args = parser().parse_args(['web', '--bind', '0.0.0.0', '--allow-host', 'qnbot-macmini.qnbot.net', '--no-browser'])
    def no_probe(*a, **kw):
        raise AssertionError('LAN launch must not reuse the old loopback-only instance')
    monkeypatch.setattr(module.urllib.request, 'urlopen', no_probe)
    invoked = []
    monkeypatch.setattr(module.web, 'run_app', lambda app, **kwargs: invoked.append(kwargs))
    assert module.run_web(args) == 0
    assert invoked[0]['host'] == '0.0.0.0' and invoked[0]['port'] == 8765
