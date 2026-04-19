"""
pyi_to_schema.py — Converts a Swabian TimeTagger .pyi stub into the RPC schema JSON.

Usage:
    python pyi_to_schema.py TimeTagger.pyi --blocklist blocklist.json \
                            --out timetagger_rpc_schema.json

    # To see what changed between two library versions:
    python pyi_to_schema.py TimeTagger_new.pyi --blocklist blocklist.json \
                            --diff timetagger_rpc_schema.json
"""

import ast
import json
import argparse
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# SWIG / C++ → JSON schema type mapping
# ---------------------------------------------------------------------------

# Patterns that appear in SWIG-generated pyi return/argument types
_VECTOR_TYPES = {
    "int":       "int_array",
    "long long": "int64_array",
    "uint64_t":  "uint64_array",
    "double":    "float_array",
    "float":     "float_array",
    "string":    "string_array",
}

def _map_type(annotation: str | None) -> dict:
    """Map a pyi type annotation string to a JSON schema type fragment."""
    if annotation is None or annotation in ("Incomplete", "None", "void"):
        return {"type": "null"}

    t = annotation.strip().strip("'\"")

    # Primitive mappings
    primitives = {
        "bool":     {"type": "boolean"},
        "int":      {"type": "integer"},
        "float":    {"type": "number"},
        "double":   {"type": "number"},
        "str":      {"type": "string"},
        "void":     {"type": "null"},
        "long long":{"$ref": "#/$defs/int64"},
        "int64_t":  {"$ref": "#/$defs/int64"},
        "uint64_t": {"$ref": "#/$defs/uint64"},
        "uint32_t": {"$ref": "#/$defs/uint32"},
        "int32_t":  {"$ref": "#/$defs/int32"},
        "std::string":       {"type": "string"},
        "std::string const &": {"type": "string"},
        "ptrdiff_t": {"type": "integer"},
        "size_t":    {"type": "integer"},
        "char":      {"type": "integer"},
    }
    if t in primitives:
        return primitives[t]

    # std::vector<T> — map to the right array type
    if "std::vector" in t:
        for cpp_type, schema_ref in _VECTOR_TYPES.items():
            if cpp_type in t:
                return {"$ref": f"#/$defs/{schema_ref}"}
        return {"$ref": "#/$defs/int_array"}  # safe fallback for unknown vectors

    # Known TT class names that become handles on the wire
    # (populated later with the actual class list; handled in caller)
    return {"type": "string", "_raw_pyi": t}   # caller resolves unknown names


def _param_to_schema(arg: ast.arg, default=None) -> dict:
    """Convert a single ast.arg to a param dict."""
    annotation = ast.unparse(arg.annotation) if arg.annotation else None
    schema_type = _map_type(annotation)
    param = {"name": arg.arg}
    param.update(schema_type)
    if default is not None:
        try:
            param["default"] = ast.literal_eval(default)
        except Exception:
            param["default"] = ast.unparse(default)
        param["optional"] = True
    return param


def _extract_params(func: ast.FunctionDef) -> list:
    """Extract parameter list from a function def, skipping 'self'."""
    args = func.args
    all_args = args.args
    defaults = args.defaults

    # Pad defaults to align with the end of args
    n_args = len(all_args)
    n_defaults = len(defaults)
    padded = [None] * (n_args - n_defaults) + list(defaults)

    params = []
    for arg, default in zip(all_args, padded):
        if arg.arg in ("self", "cls"):
            continue
        params.append(_param_to_schema(arg, default))

    # *args present → mark as variadic
    if args.vararg:
        return None   # signals params_variable=True

    return params


def _extract_return(func: ast.FunctionDef) -> dict:
    if func.returns is None:
        return {"type": "null"}
    return _map_type(ast.unparse(func.returns))


# ---------------------------------------------------------------------------
# .pyi AST parser
# ---------------------------------------------------------------------------

class PYIParser:
    def __init__(self, source: str):
        self.tree = ast.parse(source)

    def classes(self) -> dict:
        """Return {class_name: {bases, methods}} from the top-level AST."""
        result = {}
        for node in self.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(b) for b in node.bases]
            methods = {}
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods[item.name] = item
                # Properties / class attributes with annotations
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    methods[item.target.id] = item   # treated as property
            result[node.name] = {"bases": bases, "methods": methods}
        return result

    def functions(self) -> dict:
        """Return {func_name: FunctionDef} for module-level functions."""
        return {
            node.name: node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
        }


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------

class SchemaBuilder:
    def __init__(self, parsed_classes: dict, parsed_functions: dict, blocklist: dict):
        self.classes    = parsed_classes
        self.functions  = parsed_functions
        self.bl         = blocklist
        self.excluded_classes   = set(blocklist.get("excluded_classes", {}).keys())
        self.excluded_functions = set(blocklist.get("excluded_functions", {}).keys())
        self.abstract_bases     = set(blocklist.get("abstract_bases", {}).get("classes", []))
        self.data_object_classes= set(blocklist.get("data_object_classes", {}).get("classes", []))
        self.ndarray_returns    = blocklist.get("type_overrides", {}).get("ndarray_returns", {})
        self.handle_returns     = blocklist.get("type_overrides", {}).get("handle_returns", {})
        self.constructor_fns    = blocklist.get("constructor_functions", {})
        self.excluded_methods   = blocklist.get("excluded_methods", {})

        # All class names that become handles (for type resolution)
        self._handle_class_names = (
            set(parsed_classes.keys())
            - self.excluded_classes
            - self.abstract_bases
        )

    # ---- helpers -----------------------------------------------------------

    def _method_excluded(self, class_name: str, method_name: str) -> bool:
        global_excluded = set(self.excluded_methods.get("*", []))
        class_excluded  = set(self.excluded_methods.get(class_name, []))
        return method_name in global_excluded or method_name in class_excluded

    def _resolve_return(self, class_name: str, method_name: str,
                        raw_return: dict) -> dict:
        """Apply overrides: void→ndarray, class name→handle."""
        # ndarray override
        if method_name in self.ndarray_returns.get(class_name, []):
            return {"$ref": "#/$defs/ndarray"}

        # handle override
        hr = self.handle_returns.get(class_name, {}).get(method_name)
        if hr:
            if hr.endswith("[]"):
                return {"type": "array", "items": {"$ref": "#/$defs/handle"},
                        "returns_class": hr[:-2]}
            return {"$ref": "#/$defs/handle", "returns_class": hr}

        # If raw pyi annotation is a known handle class, emit handle
        raw = raw_return.get("_raw_pyi", "")
        if raw in self._handle_class_names:
            return {"$ref": "#/$defs/handle", "returns_class": raw}

        # Strip internal _raw_pyi tag before returning
        result = {k: v for k, v in raw_return.items() if k != "_raw_pyi"}
        return result or {"type": "null"}

    def _build_method(self, class_name: str, method_name: str,
                       func_node: ast.FunctionDef) -> dict:
        params = _extract_params(func_node)
        raw_ret = _extract_return(func_node)
        ret = self._resolve_return(class_name, method_name, raw_ret)

        entry: dict = {}
        if params is None:
            entry["params_variable"] = True
        else:
            entry["params"] = params
        entry["returns"] = ret
        return entry

    def _build_property(self, class_name: str, prop_name: str,
                         node: ast.AnnAssign) -> dict:
        annotation = ast.unparse(node.annotation) if node.annotation else None
        return _map_type(annotation)

    # ---- public API --------------------------------------------------------

    def build(self) -> dict:
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id":     "swabian_timetagger_rpc",
            "title":   "Swabian TimeTagger msgpack-RPC Schema",
            "description": (
                "Generated by pyi_to_schema.py from the .pyi type stub and blocklist.json. "
                "Do not edit directly — re-run the converter after updating the .pyi. "
                "Presence in this schema means the item is on the RPC surface. "
                "Exclusion policy lives in blocklist.json."
            ),
            "$defs":   self._build_defs(),
            "excluded":        self._build_excluded_section(),
            "functions":       self._build_functions(),
            "abstract_bases":  self._build_abstract_bases(),
            "classes":         self._build_classes(),
        }
        return schema

    def _build_defs(self) -> dict:
        return {
            "handle":       {"type": "integer", "description": "Opaque server-side object handle."},
            "int64":        {"type": "integer", "format": "int64"},
            "uint64":       {"type": "integer", "format": "uint64", "minimum": 0},
            "uint32":       {"type": "integer", "format": "uint32", "minimum": 0},
            "int32":        {"type": "integer", "format": "int32"},
            "ndarray":      {
                "type": "object",
                "description": "Numpy array: {dtype, shape, data(bytes)}.",
                "properties": {
                    "dtype": {"type": "string"},
                    "shape": {"type": "array", "items": {"type": "integer"}},
                    "data":  {"type": "string", "contentEncoding": "base64"},
                },
                "required": ["dtype", "shape", "data"],
            },
            "int_array":    {"type": "array", "items": {"type": "integer"}},
            "int64_array":  {"type": "array", "items": {"$ref": "#/$defs/int64"}},
            "uint64_array": {"type": "array", "items": {"$ref": "#/$defs/uint64"}},
            "float_array":  {"type": "array", "items": {"type": "number"}},
            "string_array": {"type": "array", "items": {"type": "string"}},
        }

    def _build_excluded_section(self) -> dict:
        reasons = self.bl.get("excluded_classes", {})
        return {
            "_description": (
                "Items not available on the RPC surface. "
                "reason: swig_internal, callback, abstract, dangerous, excluded."
            ),
            "classes":  {k: {"reason_category": v} for k, v in reasons.items()},
            "functions": self.bl.get("excluded_functions", {}),
            "methods":   {
                k: v for k, v in self.bl.get("excluded_methods", {}).items()
                if k != "*"
            },
        }

    def _build_functions(self) -> dict:
        out = {}
        for name, node in self.functions.items():
            if name in self.excluded_functions:
                continue
            if name.startswith("_"):
                continue
            params = _extract_params(node)
            raw_ret = _extract_return(node)
            ret_class = self.constructor_fns.get(name)
            if ret_class:
                ret = {"$ref": "#/$defs/handle", "returns_class": ret_class}
            else:
                ret = {k: v for k, v in raw_ret.items() if k != "_raw_pyi"}

            entry: dict = {}
            if params is None:
                entry["params_variable"] = True
            else:
                entry["params"] = params
            entry["returns"] = ret
            out[name] = entry
        return out

    def _build_method_set(self, class_name: str, method_nodes: dict,
                           include_properties: bool = False) -> dict:
        methods = {}
        for method_name, node in method_nodes.items():
            if self._method_excluded(class_name, method_name):
                continue
            if method_name.startswith("_") and method_name not in ("__init__",):
                continue
            if method_name == "__init__":
                continue  # constructors handled separately
            if isinstance(node, ast.FunctionDef):
                methods[method_name] = self._build_method(class_name, method_name, node)
            elif include_properties and isinstance(node, ast.AnnAssign):
                methods[method_name] = self._build_property(class_name, method_name, node)
        return methods

    def _build_abstract_bases(self) -> dict:
        out = {}
        for class_name in self.abstract_bases:
            if class_name not in self.classes:
                continue
            info = self.classes[class_name]
            bases = [b for b in info["bases"] if b not in ("object",)]
            methods = self._build_method_set(class_name, info["methods"])
            entry: dict = {}
            if bases:
                entry["inherits"] = bases[0] if len(bases) == 1 else bases
            entry["methods"] = methods
            out[class_name] = entry
        return out

    def _build_classes(self) -> dict:
        out = {}
        for class_name, info in self.classes.items():
            if class_name in self.excluded_classes:
                continue
            if class_name in self.abstract_bases:
                continue

            bases = [b for b in info["bases"]
                     if b not in ("object",) and not b.startswith("_")]

            # Constructor params (from __init__)
            init_node = info["methods"].get("__init__")
            constructor: dict | None = None
            if init_node and isinstance(init_node, ast.FunctionDef):
                ctor_params = _extract_params(init_node)
                if ctor_params is None:
                    constructor = {"params_variable": True}
                elif ctor_params:
                    constructor = {"params": ctor_params}

            # Override constructor to factory function name if applicable
            ctor_fn = next(
                (fn for fn, cls in self.constructor_fns.items() if cls == class_name),
                None
            )

            methods = self._build_method_set(class_name, info["methods"],
                                              include_properties=True)

            # Add discard() for DataObject classes
            if class_name in self.data_object_classes:
                methods["discard"] = {
                    "params": [],
                    "returns": {"type": "null"},
                    "note": "Explicit server-side handle release.",
                }

            entry: dict = {}
            if bases:
                entry["inherits"] = bases[0] if len(bases) == 1 else bases
            if ctor_fn:
                entry["constructor"] = ctor_fn
            elif constructor:
                entry["constructor"] = constructor
            entry["methods"] = methods
            out[class_name] = entry

        return out


# ---------------------------------------------------------------------------
# Diff reporter
# ---------------------------------------------------------------------------

def _flatten_schema(schema: dict) -> set[str]:
    """Return a flat set of dotted paths present in the schema for diffing."""
    paths = set()
    for class_name, cls in schema.get("classes", {}).items():
        paths.add(f"class:{class_name}")
        for m in cls.get("methods", {}):
            paths.add(f"method:{class_name}.{m}")
    for fn in schema.get("functions", {}):
        paths.add(f"function:{fn}")
    return paths


def diff_schemas(old: dict, new: dict) -> dict:
    old_paths = _flatten_schema(old)
    new_paths = _flatten_schema(new)
    return {
        "added":   sorted(new_paths - old_paths),
        "removed": sorted(old_paths - new_paths),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pyi",       help="Path to TimeTagger .pyi stub")
    parser.add_argument("--blocklist", default="blocklist.json",
                        help="Path to blocklist.json (default: blocklist.json)")
    parser.add_argument("--out",     help="Write generated schema to this file")
    parser.add_argument("--diff",    help="Path to existing schema; print what changed and exit")
    args = parser.parse_args()

    pyi_source  = Path(args.pyi).read_text()
    blocklist   = json.loads(Path(args.blocklist).read_text())

    p = PYIParser(pyi_source)
    builder = SchemaBuilder(p.classes(), p.functions(), blocklist)
    schema = builder.build()

    if args.diff:
        old_schema = json.loads(Path(args.diff).read_text())
        changes = diff_schemas(old_schema, schema)
        if not changes["added"] and not changes["removed"]:
            print("No API surface changes detected.")
        else:
            if changes["added"]:
                print("ADDED:")
                for item in changes["added"]:
                    print(f"  + {item}")
            if changes["removed"]:
                print("REMOVED:")
                for item in changes["removed"]:
                    print(f"  - {item}")
        sys.exit(0)

    output = json.dumps(schema, indent=2)

    if args.out:
        Path(args.out).write_text(output)
        print(f"Schema written to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
