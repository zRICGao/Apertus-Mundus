"""Opt-in LAN pairing and a small revision clock shared by all browser clients."""
import io
import os
import ipaddress
import secrets
import socket
import threading
import time
import json
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect


def container_local_mode():
    # Only enable behind the provided loopback-only Docker port mapping.
    return os.environ.get('RPG_ACCESS_MODE') == 'container-local'


def is_local(host):
    if container_local_mode():
        return True
    # TestClient is an in-process ASGI transport, never a remote TCP hostname.
    if host == 'testclient':
        return True
    try:
        address = ipaddress.ip_address(host or '')
        return address.is_loopback or (address.version == 6 and address.ipv4_mapped is not None
                                       and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def lan_addresses():
    candidates = []
    try:
        # UDP connect selects the default local interface without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 80))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(info[4][0] for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM))
    except OSError:
        pass
    result = []
    for host in candidates:
        address = ipaddress.ip_address(host)
        if address.is_private and not address.is_loopback and not address.is_link_local and host not in result:
            result.append(host)
    return result


class DeviceSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.instance = secrets.token_hex(8)
        self.revision = 0
        self.clients = {}

    def allows(self, token):
        with self.lock:
            return bool(self.token and token and token.isascii() and len(token) <= 128
                        and secrets.compare_digest(self.token, token))

    def enable(self):
        with self.lock:
            if not self.token:
                self.token = secrets.token_urlsafe(32)

    def disable(self):
        with self.lock:
            self.token = None
            self.clients.clear()

    def changed(self):
        with self.lock:
            self.revision += 1

    def status(self, client='', kind='workbench'):
        now = time.monotonic()
        with self.lock:
            self.clients = {key: value for key, value in self.clients.items() if now - value[0] < 20}
            if client and len(client) <= 80 and kind in ('workbench', 'phone'):
                self.clients[client] = (now, kind)
            return {'enabled': bool(self.token), 'revision': f'{self.instance}:{self.revision}',
                    'connected_devices': len(self.clients)}


class DeviceAccessMiddleware:
    def __init__(self, app, session):
        self.app, self.session = app, session

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        local = is_local((scope.get('client') or ('', 0))[0])
        auth_token = ''
        if not local:
            from http.cookies import SimpleCookie
            headers = dict(scope.get('headers', []))
            cookie = SimpleCookie()
            try:
                cookie.load(headers.get(b'cookie', b'').decode('latin1'))
            except Exception:
                cookie = SimpleCookie()
            token = cookie.get('apertus_device')
            auth_token = token.value if token else ''
            paired = self.session.allows(auth_token)
            if not paired and scope.get('path') != '/join':
                if scope['type'] == 'websocket':
                    return await send({'type': 'websocket.close', 'code': 1008})
                response = HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>连接 Apertus Mundus</title><body style="font:18px system-ui;padding:32px;line-height:1.8">'
                    '<h1>请重新扫码连接</h1><p>在电脑端打开「手机连接与同步」，开启连接后扫描二维码。'
                    '连接关闭或电脑服务重启后，需要重新扫码。</p></body></html>', status_code=403,
                    headers={'Cache-Control': 'no-store'})
                return await response(scope, receive, send)

        status = 500
        mutation = (scope['type'] == 'http' and scope.get('method') in ('POST', 'PUT', 'PATCH', 'DELETE')
                    and scope.get('path', '').startswith('/api/')
                    and not scope['path'].startswith(('/api/devices/', '/api/config')))

        async def observe(message):
            nonlocal status
            if scope['type'] == 'websocket' and not local and message['type'] != 'websocket.close' and not self.session.allows(auth_token):
                await send({'type': 'websocket.close', 'code': 1008})
                raise WebSocketDisconnect(1008)
            if message['type'] == 'http.response.start':
                status = message['status']
            if mutation and message['type'] == 'http.response.body' and not message.get('more_body') and status < 400:
                self.session.changed()
            await send(message)

        await self.app(scope, receive, observe)


def create_router(session, current_scene):
    router = APIRouter()

    def local_only(request):
        if not is_local(request.client.host if request.client else ''):
            raise HTTPException(403, '请在运行服务的电脑上管理连接')
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403, '请从本机连接页面操作')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '请从本机连接页面操作')

    def link_info(request, address='', view='workbench'):
        local_only(request)
        if container_local_mode():
            raise HTTPException(409, '当前只开放本机工作台。手机连接请使用桌面启动方式。')
        addresses = lan_addresses()
        if address and address not in addresses:
            raise HTTPException(400, '所选网络地址已变化，请刷新连接页')
        if view not in ('workbench', 'phone'):
            raise HTTPException(400, '未知的手机入口')
        host = address or next(iter(addresses), '')
        port = request.url.port or 8000
        with session.lock:
            token = session.token
        url = f'http://{host}:{port}/join?' + urlencode({'token': token, 'view': view}) if host and token else ''
        return {**session.status(), 'addresses': addresses, 'address': host, 'url': url,
                'local': True, 'view': view}

    @router.get('/api/devices/state')
    def state(request: Request, client: str = '', kind: str = 'workbench'):
        return {**session.status(client, kind), 'current_scene_id': current_scene(),
                'container_local': container_local_mode(),
                'local': is_local(request.client.host if request.client else '')}

    @router.get('/api/devices/link')
    def link(request: Request, address: str = '', view: str = 'workbench'):
        return Response(json.dumps(link_info(request, address, view)),
                        media_type='application/json', headers={'Cache-Control': 'no-store'})

    @router.post('/api/devices/enable')
    def enable(request: Request):
        local_only(request)
        if container_local_mode():
            raise HTTPException(409, '当前只开放本机工作台。手机连接请使用桌面启动方式。')
        if not lan_addresses():
            raise HTTPException(409, '没有找到局域网地址。请连接 Wi-Fi 或网线后重试。')
        session.enable()
        return link_info(request)

    @router.post('/api/devices/disable')
    def disable(request: Request):
        local_only(request)
        session.disable()
        return session.status()

    @router.get('/api/devices/qr.svg')
    def qr(request: Request, address: str = '', view: str = 'workbench'):
        info = link_info(request, address, view)
        if not info['url']:
            raise HTTPException(409, '请先开启手机连接')
        import qrcode
        import qrcode.image.svg
        image = qrcode.make(info['url'], image_factory=qrcode.image.svg.SvgPathImage,
                            error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
        stream = io.BytesIO()
        image.save(stream)
        return Response(stream.getvalue(), media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})

    @router.get('/join')
    def join(token: str = '', view: str = 'workbench'):
        if not session.allows(token):
            raise HTTPException(403, '连接已失效，请在电脑上重新开启连接并扫码')
        target = '/phone.html' if view == 'phone' else '/index.html?resume=1'
        response = RedirectResponse(target, status_code=303, headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('apertus_device', token, httponly=True, samesite='lax', path='/')
        return response

    return router
