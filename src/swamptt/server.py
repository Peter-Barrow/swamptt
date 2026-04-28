import asyncio
import itertools
import logging
import logging.handlers
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import msgpack
from dc_parse import create_config_hierarchy

from .server_handlers import HANDLERS, HandlerContext

registry = {
    '_counter': itertools.count(1),  # look up counter when requesting a ctor,
    # itertools.count() gives a monotonic id for all ctor calls, the value is
    # stored through the HandlerContext
}

sessions = {}  # generate a unique session id for all connections, this id will
# also be accessed via HandlerContext and end up in the registry

logger = logging.getLogger(__name__)


def _listener() -> logging.handlers.QueueListener:
    log_queue = queue.SimpleQueue()

    queue_handler = logging.handlers.QueueHandler(log_queue)
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger().addHandler(queue_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(
        logging.Formatter(
            '%(asctime)s [%(levelname)-8s] %(threadName)s: %(message)s',
        ),
    )

    return logging.handlers.QueueListener(
        log_queue,
        stream_handler,
        respect_handler_level=True,
    )


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
):
    session_id = id(
        writer
    )  # this should give us the unique id since python creates them we may as well reuse

    sessions[session_id] = set()
    unpacker = msgpack.Unpacker(raw=False)
    loop = asyncio.get_running_loop()

    addr = writer.get_extra_info('peername')
    logger.info(f'-> connection from {addr}')

    try:
        while chunk := await reader.read(65536):
            unpacker.feed(chunk)
            for msg in unpacker:
                # msgpack-rpc request: [type=0, msgid, method, params]
                msgid, method, params = msg[1], msg[2], msg[3]
                ctx = HandlerContext(session_id, registry, sessions)
                logger.info(
                    f'-> session: {session_id}:{msgid} requested {method}'
                )
                try:
                    result = await loop.run_in_executor(
                        executor,
                        HANDLERS[method],
                        ctx,
                        params,
                    )
                    response = [1, msgid, None, result]
                except Exception as e:
                    response = [1, msgid, str(e), None]
                    logger.error(
                        f'-> session: {session_id}:{msgid} requested {method} -> failed {e}'
                    )

                writer.write(msgpack.packb(response, use_bin_type=True))
                await writer.drain()
    finally:
        # clean up, remove handles and close writer, should be enough
        for handle_id in sessions.pop(session_id, ()):
            registry.pop(handle_id, None)
        writer.close()


@dataclass
class ServerConfig:
    server_name: str = 'swamptt-server'
    workers: int = 1
    ip_addr: str = 'localhost'
    port: int = 9000


async def _async_main(config: ServerConfig):
    global executor
    executor = ThreadPoolExecutor(
        max_workers=config.workers,
        thread_name_prefix=f'{config.server_name}-worker',
    )
    logger.info(f'Server initialised with {config.workers} worker(s)')

    # Add this if you want to use run_in_executor(None, ...) elsewhere —
    # "None" means "use the default", so this makes your named executor
    # the default. Skip it if you always pass the executor explicitly.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)

    server = await asyncio.start_server(
        handle_client,
        config.ip_addr,
        config.port,
    )
    logger.info(f'Server listening at {config.ip_addr}:{config.port}')
    async with server:
        await server.serve_forever()


def _get_local_ip():
    from socket import AF_INET, SOCK_DGRAM, socket
    s = socket(AF_INET, SOCK_DGRAM)
    local_ip : str | None = None
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()

    return local_ip


def main():

    parser, parse_fn = create_config_hierarchy(
        ServerConfig,
    )

    config = parse_fn()
    if config is None:
        return

    config = config['ServerConfig']

    if config.ip_addr == 'localhost':
        local_ip = _get_local_ip()
        if local_ip is not None:
            config.ip_addr = local_ip


    listener = _listener()
    listener.start()
    logger.info(f'SWAMPTT server, "{config.server_name}", started')
    try:
        asyncio.run(_async_main(config))
    except KeyboardInterrupt:
        logger.info('Shutting down server')
    finally:
        listener.stop()


if __name__ == '__main__':
    main()
