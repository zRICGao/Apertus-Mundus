"""Reserve the HTTP port before opening a browser, including on Windows."""
import socket
import threading
import time
import webbrowser

import uvicorn


def reserve_listener(first_port=8000, attempts=20):
    for port in range(first_port, first_port + attempts):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Windows otherwise permits a wildcard listener to overlap an existing
            # loopback listener, sending the browser to an unrelated application.
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('0.0.0.0', port))
            listener.listen(128)
            listener.setblocking(False)
            return listener
        except OSError:
            listener.close()
    raise OSError(f'无法使用端口 {first_port}–{first_port + attempts - 1}，请关闭旧后端后重试。')


def open_when_ready(server, url):
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.1)
    webbrowser.open(url)


def run_local_server(app, logger):
    with reserve_listener() as listener:
        port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}/connect'
        if port != 8000:
            logger.warning('8000 端口已被占用，改用端口 %d。', port)
        logger.info('浏览器入口：%s', url)
        server = uvicorn.Server(uvicorn.Config(
            app, host='0.0.0.0', port=port, log_level='info', proxy_headers=False))
        threading.Thread(target=open_when_ready, args=(server, url), daemon=True).start()
        try:
            server.run(sockets=[listener])
        finally:
            server.should_exit = True
