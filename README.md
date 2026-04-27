<div align="center">

  <img src="assets/swamptt-text.png" alt="swamptt logo" width="300"/>
  <p><em>This is my swamp. And my timetagger.</em></p>
  
</div>

## About

This is a small RPC server for the Swabian Instruments TimeTaggers, inspired by the [server provided by Swabian Instruments](https://github.com/swabianinstruments/TimeTaggerRPC). Instead of using Pyro like they have this instead makes use of msgpack to move data between server and client, as bonus this means you can write your own client in your language of choice should you so please.

The motivation for making this was to:
1. make an rpc server
2. their library lacked type-hints.

So naturally the wheel was reinvented, there's layers to it.

## Install

Not available on PyPi yet (maybe never) so for now installation from git as follows:

```sh
python3 -m pip install git+https://github.com/Peter-Barrow/swamptt.git
```

Or maybe a little sunshine in your swamp:

```sh
uv add git+https://github.com/Peter-Barrow/swamptt.git
```


## Running the server

The server is available to run as a module

```sh
python3 -m swamptt.server
```

Alternatively, with uv:

```
uv run swamptt-server
```


## Connecting as a client

Usage for clients has been made to mimic the [official library](https://pypi.org/project/Swabian-TimeTagger/), all you need to do is change the import to `import swamptt.client as TT` and make the connection. From there this should feel no different than using the official library on a supported platform, (but now you can also use the TimeTaggers from macOS too).

You can put something like the following in your imports to make your scripts cross platform.

```python

import sys
is_rpc = False
if sys.platform == 'linux':
    from Swabian import TimeTagger as TT
elif sys.platform == 'win32':
    from Swabian import TimeTagger as TT
else:
    import swamptt.client as TT
    is_rpc = True

# some very import measurement here ...

if __name__ == '__main__':
    # establish the rpc connection if we need to
    if is_rpc:
        _conn = TT.connection(ip_addr, port)

```

Or more robustly, using a try/except:

```python
is_rpc = False
try:
    from Swabian import TimeTagger as TT
except ImportError:
    import swamptt.client as TT
    is_rpc = True

# some very import measurement here ...

if __name__ == '__main__':
    # establish the rpc connection if we need to
    if is_rpc:
        _conn = TT.connection(ip_addr, port)
```


And usage looks no different than if you were using the *real thing*™️.

```python

import swamptt.client as TT
import matplotlib.pyplot as plt

ip_addr = '192.168.64.8'
port = 9000

_connection = TT.connect(ip_addr, port)

virt = TT.createTimeTaggerVirtual('test-data.ttbin')

virt.run()

cr = TT.Countrate(virt, [1, 2, 3])
sleep(1)
print(cr.getData())
```
