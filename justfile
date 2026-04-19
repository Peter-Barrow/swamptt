default:
    just --list

build-schema:
    uv run code-gen/schema-from-pyi.py \\
        code-gen/sources/Swabian.pyi \\
        --blocklist code-gen/sources/blocklist.json \\
        --out code-gen/sources/generated-schema.json

generate-server:
    uv run code-gen/codegen.py \\
        code-gen/sources/generated-schema.json \\
        --out-dir src/swamptt/

code-gen: build-schema generate-server

clean:
    rm code-gen/sources/generated-schema.json
    rm src/swamptt/server_handlers.py
    rm src/swamptt/client_stubs.py
