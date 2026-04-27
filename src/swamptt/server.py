import asyncio
import itertools
import logging
import logging.handlers
import queue
import sys
from concurrent.futures import ThreadPoolExecutor

import msgpack

from .server_handlers import HANDLERS, HandlerContext, _lookup

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


async def _async_main():
    global executor
    executor = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix='swamptt-server-worker',
    )

    # Add this if you want to use run_in_executor(None, ...) elsewhere —
    # "None" means "use the default", so this makes your named executor
    # the default. Skip it if you always pass the executor explicitly.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)

    port = 9000
    server = await asyncio.start_server(handle_client, '0.0.0.0', port)
    logger.info(f'Server listening on port {port}')
    async with server:
        await server.serve_forever()


def main():
    listener = _listener()
    listener.start()
    logger.info('SWAMPTT server started')
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info('Shutting down server')
    finally:
        listener.stop()


if __name__ == '__main__':
    main()
